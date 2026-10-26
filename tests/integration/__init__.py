def agent_runtime(build='3.5.0', *, image=None, health='valid'):
    return {'protocol':'edbg.agent/v1','family':'esp32s3','build':build,'uptime_ms':1000,'health':health,
            'image':image or {'algorithm':'sha256-file-v1','sha256':'a'*64,'size':512,'project':'fleet_agent'},
            'features':['runtime.image/1','node.update/1','uart.fixed/1','i2c.fixed/1','wave.fixed/1']}
