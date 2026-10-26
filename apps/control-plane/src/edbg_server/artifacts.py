"""Content-addressed node firmware catalog with structural and checksum validation."""
import hashlib
import os
import re
import secrets
import time
import struct
from functools import reduce
from operator import xor

from fastapi import Depends, HTTPException, Query, Request

from edbg_server.workspace import reject

# Bound by the application after resolving its state directory; importing a
# client/test module first must not select a different private image store.
STATE = None
MAX_IMAGE = 0x300000


def image_metadata(data):
    if len(data) < 256 or len(data) > MAX_IMAGE or data[0] != 0xE9 or int.from_bytes(data[12:14], 'little') != 9:
        raise HTTPException(400, '请选择不超过 3 MB 的 ESP32-S3 应用固件')
    if data[32:36] != bytes.fromhex('3254cdab'):
        raise HTTPException(400, '应用描述信息无效')
    try:
        version = data[48:80].split(b'\0')[0].decode('ascii', errors='strict')
        project = data[80:112].split(b'\0')[0].decode('ascii', errors='strict')
    except UnicodeError:
        raise HTTPException(400, '固件描述无效')
    if project != 'fleet_agent' or not version or len(version) > 31:
        raise HTTPException(400, '固件必须来自 fleet_agent 工程')
    # Canonical unsigned ESP app image: all segments, aligned checksum and its
    # appended SHA-256. Trailers/signatures require a separate explicit format.
    if not 1<=data[1]<=16 or data[23]!=1:
        raise HTTPException(400,'固件需要有效的分段和附加 SHA-256')
    offset,checksum=24,0xef
    for index in range(data[1]):
        if offset+8>len(data):raise HTTPException(400,'固件分段头被截断')
        _,length=struct.unpack_from('<II',data,offset);offset+=8
        if index==0 and length<256:raise HTTPException(400,'应用描述分段太短')
        if length>MAX_IMAGE or offset+length>len(data):raise HTTPException(400,'固件分段长度无效')
        checksum=reduce(xor,memoryview(data)[offset:offset+length],checksum)
        offset+=length
    end=(offset+16)&~15
    if len(data)!=end+32 or data[end-1]!=checksum or hashlib.sha256(data[:end]).digest()!=data[end:]:
        raise HTTPException(400,'固件长度、校验和或 SHA-256 无效')
    return version


def path(identity):
    if not re.fullmatch('[0-9a-f]{24}', identity):
        reject('ARTIFACT_NOT_FOUND', 'Artifact was not found.', 404)
    if STATE is None:raise RuntimeError('Artifact store has not been configured')
    return STATE / 'firmware' / (identity + '.bin')


def lookup(c, identity):
    return c.execute("""SELECT f.*,COALESCE(w.reason,'') AS withdrawn FROM firmware f
        LEFT JOIN firmware_withdrawals w ON w.firmware_id=f.id WHERE f.id=?""", (identity,)).fetchone()


def view(row):
    return {'api_version':'edbg.artifact/v0','id':row['id'],'kind':'node_firmware',
            'sha256':row['sha256'],'size':row['size'],'version':row['version'],'created':row['created'],
            'format':'esp_app','chip':'esp32s3','project':'fleet_agent',
            'withdrawn':bool(row['withdrawn']),'withdrawal_reason':row['withdrawn'],
            'validation':'esp_image_segments_checksum_sha256','signature_verified':False}


def check_file(row):
    try:
        with path(row['id']).open('rb') as stream:
            data=stream.read(MAX_IMAGE+1)
        return (len(data)==row['size'] and hashlib.sha256(data).hexdigest()==row['sha256']
                and image_metadata(data)==row['version'])
    except (OSError,HTTPException):
        return False


def store(c, data):
    version=image_metadata(data);sha=hashlib.sha256(data).hexdigest();identity=sha[:24]
    previous=lookup(c,identity)
    if previous and previous['sha256']!=sha:
        reject('ARTIFACT_ID_CONFLICT','Artifact identity prefix collides with another digest.')
    destination=path(identity);destination.parent.mkdir(parents=True,exist_ok=True)
    if previous and not check_file(previous):
        reject('ARTIFACT_INTEGRITY_FAILED','Stored artifact is missing or differs from its recorded identity.')
    if not previous:
        temporary=destination.with_name(identity+'.'+secrets.token_hex(8)+'.local.bin')
        try:
            with temporary.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,destination)
        finally:
            temporary.unlink(missing_ok=True)
        c.execute('INSERT INTO firmware VALUES(?,?,?,?,?)',(identity,version,sha,len(data),time.time()))
    return lookup(c,identity)


async def read_image(request):
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>MAX_IMAGE:
            reject('ARTIFACT_TOO_LARGE','Node firmware limit is 3 MiB.',413)
        data.extend(chunk)
    return data


def register(app,db,admin,audit):
    @app.get('/api/workspace/artifacts',dependencies=[Depends(admin)])
    def listing(before:str='',limit:int=Query(default=50,ge=1,le=200)):
        with db() as c:
            where,args='',[]
            if before:
                cursor=lookup(c,before)
                if cursor is None:reject('ARTIFACT_NOT_FOUND','Artifact cursor was not found.',404)
                where='WHERE (f.created,f.id)<(?,?)';args=[cursor['created'],cursor['id']]
            rows=c.execute("SELECT f.*,COALESCE(w.reason,'') AS withdrawn FROM firmware f LEFT JOIN firmware_withdrawals w ON w.firmware_id=f.id "+where+' ORDER BY f.created DESC,f.id DESC LIMIT ?',(*args,limit+1)).fetchall()
            return {'items':[view(r) for r in rows[:limit]],'next_before':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/api/workspace/artifacts/{identity}',dependencies=[Depends(admin)])
    def show(identity:str):
        with db() as c:
            row=lookup(c,identity)
            if row is None:reject('ARTIFACT_NOT_FOUND','Artifact was not found.',404)
            return view(row)

    @app.post('/api/workspace/artifacts',dependencies=[Depends(admin)])
    async def upload(request:Request):
        data=await read_image(request)
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                row=store(c,data);audit(c,'workspace_artifact_import',row['id'])
                return view(row)
        except HTTPException as error:
            if isinstance(error.detail,str):
                reject('ARTIFACT_INVALID','Expected a supported ESP32-S3 fleet_agent application image.',422)
            raise
