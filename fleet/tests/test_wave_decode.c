#include <assert.h>
#include <stdio.h>
#include "../firmware/main/wave_decode.h"
#define HALF(level, us) (((uint32_t)(level)<<15)|(us))
#define WORD(l0,d0,l1,d1) (HALF(l0,d0)|(HALF(l1,d1)<<16))
int main(void) {
    wave_segment_t result[8];
    const uint32_t stale_eof[] = {WORD(1,500,0,500),WORD(1,500,0,4457),WORD(0,0,1,853)};
    assert(wave_decode(stale_eof,3,result,8)==4);
    assert(result[3].level==0 && result[3].us==4457);
    assert(wave_has_pulse(stale_eof,3));
    const uint32_t idle[] = {WORD(0,20000,0,0),WORD(1,853,0,100)};
    assert(!wave_has_pulse(idle,2));
    assert(wave_decode(idle,2,result,8)==0);
    const uint32_t odd_end[] = {WORD(0,400,1,250),WORD(0,750,0,0),WORD(1,99,1,88)};
    assert(wave_decode(odd_end,3,result,8)==2);
    assert(result[0].level==1 && result[0].us==250);
    assert(result[1].level==0 && result[1].us==750);
    result[1]=(wave_segment_t){9,123};
    assert(wave_decode(stale_eof,3,result,1)==1);
    assert(result[1].level==9 && result[1].us==123);
    puts("4 RMT decode regressions passed.");
    return 0;
}
