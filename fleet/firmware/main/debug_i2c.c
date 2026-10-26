#include <stdio.h>
#include <string.h>
#include "debug_i2c.h"
#include "driver/i2c.h"
#include "driver/gpio.h"
#include "hal/i2c_ll.h"
#include "esp_timer.h"
#include "esp_rom_sys.h"

// IDF 5.3.1's new master probe hardcodes 100 kHz. Use the isolated legacy
// command-link driver so address-only scans also obey this bench's 10 kHz limit.
// No new I2C driver is linked. Pins never use push-pull outputs.
#define BUS I2C_NUM_0
#define SDA 9
#define SCL 10
#define ADDRESS 0x42
#define LIMIT 64
static bool installed, target;
static char last_id[25], state[16] = "idle", error[96];
static uint8_t received[LIMIT], found[112];
static int received_len, found_len;
static int sda_idle=-1, scl_idle=-1;
static int64_t elapsed_us, deadline;

static const char *str(cJSON *o, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static int num(cJSON *o, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsNumber(v) ? v->valueint : -1;
}
static int hex(const char *s, uint8_t *out) {
    size_t n = strlen(s);
    if (n > LIMIT*2 || n%2) return -1;
    for (size_t i=0; i<n; i++) {
        char c=s[i]; int x=c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;
        if (x<0) return -1;
        if (i%2) out[i/2]|=x; else out[i/2]=x<<4;
    }
    return n/2;
}
static void release_bus(void) {
    if (installed) i2c_driver_delete(BUS);
    installed=false;
    gpio_config_t cfg={.pin_bit_mask=(1ULL<<SDA)|(1ULL<<SCL),.mode=GPIO_MODE_INPUT,.pull_up_en=GPIO_PULLUP_DISABLE,.pull_down_en=GPIO_PULLDOWN_DISABLE,.intr_type=GPIO_INTR_DISABLE};
    gpio_config(&cfg);
}
void debug_i2c_tick(void) {
    if (installed && target && received_len<LIMIT) {
        int n=i2c_slave_read_buffer(BUS,received+received_len,LIMIT-received_len,0);
        if(n>0) received_len+=n;
    }
    if (installed && esp_timer_get_time()>=deadline) {
        release_bus(); snprintf(state,sizeof(state),"cancelled");
        snprintf(error,sizeof(error),"I2C operation lease expired");
    }
}
void debug_i2c_stop(void) {
    debug_i2c_tick();
    if(installed) {
        release_bus();
        if(!strcmp(state,"ready")) snprintf(state,sizeof(state),"cancelled");
    }
}
static esp_err_t install_bus(bool slave) {
    i2c_config_t cfg={.mode=slave?I2C_MODE_SLAVE:I2C_MODE_MASTER,.sda_io_num=SDA,.scl_io_num=SCL,.sda_pullup_en=GPIO_PULLUP_ENABLE,.scl_pullup_en=GPIO_PULLUP_ENABLE};
    if(slave) {cfg.slave.addr_10bit_en=0;cfg.slave.slave_addr=ADDRESS;cfg.slave.maximum_speed=10000;}
    else cfg.master.clk_speed=10000;
    esp_err_t e=i2c_param_config(BUS,&cfg);
    if(e==ESP_OK) e=i2c_driver_install(BUS,cfg.mode,slave?256:0,slave?256:0,0);
    if(e==ESP_OK) {
        installed=true;target=slave;deadline=esp_timer_get_time()+20000000;
        // IDF 5.3.1's legacy ISR clears coincident TX-empty/transaction-done
        // flags together. A repeated START can otherwise leave the final RX
        // bytes below the default 28-byte watermark. Drain each received byte
        // via the driver's ISR. At 10 kHz this is at most ~1111 IRQ/s, for 64 B.
        // Configure before reporting ready, while the paired master is idle.
        if(slave) {i2c_ll_set_rxfifo_full_thr(I2C_LL_GET_HW(BUS),1);i2c_ll_update(I2C_LL_GET_HW(BUS));}
    }
    return e;
}
static esp_err_t transact(int address, const uint8_t *write, int wn, uint8_t *read, int rn) {
    i2c_cmd_handle_t cmd=i2c_cmd_link_create();
    if(!cmd) return ESP_ERR_NO_MEM;
    esp_err_t e=i2c_master_start(cmd);
    // An address-only write is the probe. Read-only requests skip the write phase.
    if(e==ESP_OK && (wn || !rn)) e=i2c_master_write_byte(cmd,(address<<1)|I2C_MASTER_WRITE,true);
    if(e==ESP_OK && wn) e=i2c_master_write(cmd,write,wn,true);
    if(e==ESP_OK && wn && rn) e=i2c_master_start(cmd); // Repeated START, no intervening STOP.
    if(e==ESP_OK && rn) e=i2c_master_write_byte(cmd,(address<<1)|I2C_MASTER_READ,true);
    if(e==ESP_OK && rn>1) e=i2c_master_read(cmd,read,rn-1,I2C_MASTER_ACK);
    if(e==ESP_OK && rn) e=i2c_master_read_byte(cmd,read+rn-1,I2C_MASTER_NACK);
    if(e==ESP_OK) e=i2c_master_stop(cmd);
    if(e==ESP_OK) e=i2c_master_cmd_begin(BUS,cmd,pdMS_TO_TICKS(250));
    i2c_cmd_link_delete(cmd);
    return e;
}
void debug_i2c_control(cJSON *control, bool is_probe) {
    if(!cJSON_IsObject(control)) {debug_i2c_stop();return;}
    const char *id=str(control,"id"), *kind=str(control,"kind"), *op=str(control,"operation");
    if(strlen(id)!=24 || !strcmp(last_id,id)) return;
    debug_i2c_stop();
    // Latch before touching the bus; replayed network responses never repeat a write.
    snprintf(last_id,sizeof(last_id),"%s",id);
    received_len=found_len=0;sda_idle=scl_idle=-1;elapsed_us=0;error[0]=0;
    snprintf(state,sizeof(state),"failed");
    uint8_t write[LIMIT],reply[LIMIT];
    int wn=hex(str(control,"data"),write),rn=num(control,"read_len"),replyn=hex(str(control,"reply"),reply),address=num(control,"address");
    bool scan=!strcmp(op,"scan"),reading=!strcmp(op,"read")||!strcmp(op,"write_read"),writing=!strcmp(op,"write")||!strcmp(op,"write_read");
    const char *mode=str(control,"mode");
    bool external=!strcmp(mode,"external"),master=!strcmp(kind,"master");
    bool paired=!mode[0]||!strcmp(mode,"paired");
    int start=num(control,"scan_start"),end=num(control,"scan_end");
    if(start==-1)start=8;
    if(end==-1)end=0x77;
    if((!scan&&!reading&&!writing)||wn<0||rn<0||rn>LIMIT||replyn<0||(paired&&replyn!=rn)||(external&&replyn!=0)||(!paired&&!external)||!!wn!=writing||!!rn!=reading||address<8||address>0x77||start<8||end>0x77||start>end||num(control,"speed_hz")!=10000||num(control,"sda")!=SDA||num(control,"scl")!=SCL||num(control,"target_address")!=ADDRESS||(paired&&strcmp(kind,is_probe?"master":"target"))||(external&&!master)) {
        snprintf(error,sizeof(error),"Invalid interface parameters");return;
    }
    int64_t started=esp_timer_get_time();
    esp_err_t e=install_bus(!master);
    if(e==ESP_OK && !master) {
        if(rn && i2c_slave_write_buffer(BUS,reply,rn,pdMS_TO_TICKS(100))!=rn) e=ESP_FAIL;
        if(e==ESP_OK) {snprintf(state,sizeof(state),"ready");return;}
    }
    if(e==ESP_OK && master) {
        esp_rom_delay_us(100); // Allow enabled weak pull-ups to settle before checking idle.
        sda_idle=gpio_get_level(SDA);scl_idle=gpio_get_level(SCL);
        if(sda_idle!=1 || scl_idle!=1) {snprintf(error,sizeof(error),"Bus not idle: SDA/SCL held low");e=ESP_ERR_INVALID_STATE;}
        else if(scan) {
            for(int a=start;a<=end;a++) {
                esp_err_t probe=transact(a,NULL,0,NULL,0);
                if(probe==ESP_OK) found[found_len++]=a;
                else if(probe!=ESP_FAIL) {e=probe;break;}
            }
        } else {
            e=transact(address,write,wn,received,rn);
            if(e==ESP_OK) received_len=rn;
        }
    }
    elapsed_us=esp_timer_get_time()-started;
    release_bus();
    if(e==ESP_OK) snprintf(state,sizeof(state),"done");
    else if(!error[0]) snprintf(error,sizeof(error),"%s",e==ESP_FAIL?"NACK: address or data not acknowledged":esp_err_to_name(e));
}
void debug_i2c_snapshot(cJSON *debug) {
    debug_i2c_tick();
    cJSON *s=cJSON_AddObjectToObject(debug,"i2c");
    cJSON_AddStringToObject(s,"id",last_id);cJSON_AddStringToObject(s,"state",state);cJSON_AddStringToObject(s,"error",error);
    cJSON_AddNumberToObject(s,"elapsed_us",elapsed_us);
    if(sda_idle>=0)cJSON_AddNumberToObject(s,"sda_idle",sda_idle);
    if(scl_idle>=0)cJSON_AddNumberToObject(s,"scl_idle",scl_idle);
    char text[LIMIT*2+1]={0};
    for(int i=0;i<received_len;i++) snprintf(text+2*i,3,"%02x",received[i]);
    cJSON_AddStringToObject(s,"data",text);
    cJSON *addresses=cJSON_AddArrayToObject(s,"found");
    for(int i=0;i<found_len;i++) cJSON_AddItemToArray(addresses,cJSON_CreateNumber(found[i]));
}
