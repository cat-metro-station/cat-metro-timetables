"""Verify city bus databases, publish immutable release assets, then prepare the downloadable index."""
import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
REPO='cat-metro-station/cat-metro-timetables'
TAG='bus-packages-v1'
API='https://api.github.com/repos/'+REPO
BASE='https://github.com/'+REPO+'/releases/download/'+TAG+'/'
API_HEADERS={'Accept':'application/vnd.github+json','User-Agent':'cat-metro-bus-timetables','X-GitHub-Api-Version':'2022-11-28'}

def token():
    env=os.environ.get('GITHUB_TOKEN')
    if env:
        return env
    reply=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',capture_output=True,text=True,check=True)
    fields=dict(x.split('=',1) for x in reply.stdout.splitlines() if '=' in x)
    return fields['password']

def api(path,auth,method='GET',body=None):
    request=urllib.request.Request(API+path,data=json.dumps(body).encode() if body is not None else None,
                                    method=method,headers={**API_HEADERS,'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=40) as res:
        data=res.read()
        return json.loads(data) if data else None

def asset_list(auth,release):
    page=1
    result={}
    while True:
        assets=api('/releases/'+str(release['id'])+'/assets?per_page=100&page='+str(page),auth)
        result.update({a['name']:a for a in assets})
        if len(assets)<100:
            return result
        page+=1

def release_info(auth,create):
    try:
        return api('/releases/tags/'+TAG,auth)
    except urllib.error.HTTPError as error:
        if error.code!=404 or not create:
            raise
    return api('/releases',auth,'POST',{'tag_name':TAG,'name':'Offline bus timetables','make_latest':'false',
               'body':'Machine-readable, dated bus schedules for cat metro station. See packages/bus/COVERAGE.md for operating sources, credits, dates and known gaps.'})

def require(condition,message):
    if not condition:
        raise ValueError(message)

def validate(city,entryfile):
    require(re.fullmatch(r'[A-Za-z0-9]+',city),'invalid city')
    entry=json.loads(entryfile.read_text())
    digest=entry['sha256']
    require(re.fullmatch(r'[0-9a-f]{64}',digest),'invalid checksum')
    name=f'{city}-{digest[:16]}.sqlite.gz'
    require(entry['path']=='packages/bus/data/'+name,'unexpected asset path '+city)
    entry['url']=BASE+name
    asset=entryfile.parent/name
    require(0<asset.stat().st_size==entry['bytes']<2**31,'invalid asset size '+city)
    with asset.open('rb') as source:
        sha=hashlib.sha256()
        for chunk in iter(lambda:source.read(1024*1024),b''):
            sha.update(chunk)
    require(sha.hexdigest()==digest,'checksum mismatch '+city)
    with tempfile.TemporaryDirectory() as td:
        database=Path(td)/'pack.sqlite'
        with gzip.open(asset,'rb') as source, database.open('wb') as target:
            shutil.copyfileobj(source,target,1024*1024)
        require(database.stat().st_size==entry['uncompressedBytes'],'expanded size mismatch '+city)
        db=sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1',uri=True)
        try:
            require(db.execute('PRAGMA quick_check').fetchone()==('ok',),'SQLite integrity '+city)
            meta=dict(db.execute('SELECT key,value FROM metadata'))
            require(db.execute('PRAGMA user_version').fetchone()==(1,),'incorrect schema '+city)
            require(meta['city']==city and meta['validThrough']==entry['validThrough'],'incorrect identity '+city)
            require(meta.get('sources') and meta.get('timeZone'),'missing attribution or zone')
            check_date=dt.datetime.strptime(entry['validThrough'],'%Y%m%d').date()
            require(check_date>=dt.date.today(),'expired pack '+city)
            for col in ('stops','routes'):
                require(entry[col]>0 and db.execute('SELECT COUNT(*) FROM '+col).fetchone()[0]==entry[col],'wrong '+col)
            require(db.execute('SELECT 1 FROM calls c JOIN service_days d USING(service_id) JOIN stops s ON s.id=c.stop_id JOIN routes r ON r.id=c.route_id WHERE d.day>=? AND s.lat BETWEEN -90 AND 90 AND s.lon BETWEEN -180 AND 180 LIMIT 1',
                    (int(dt.date.today().strftime('%Y%m%d')),)).fetchone() is not None,'no dated departures '+city)
            details=json.loads(meta['sourceDetails'])
            require(details and all(v['operator'] and v['license'].startswith(('http://','https://')) and v['source'].startswith(('https://','http://')) for v in details),'missing license '+city)
        finally:
            db.close()
    return city,entry,asset,details

def put_asset(work,auth,release,known):
    city,entry,asset,_=work
    old=known.get(asset.name)
    if old:
        require(old['state']=='uploaded' and old['size']==entry['bytes'] and
                old.get('digest') in (None,'sha256:'+entry['sha256']),'asset name collision '+asset.name)
        return
    route='/repos/'+REPO+'/releases/'+str(release['id'])+'/assets?'+urllib.parse.urlencode({'name':asset.name})
    for attempt in range(4):
        try:
            conn=http.client.HTTPSConnection('uploads.github.com',timeout=180)
            with asset.open('rb') as src:
                conn.request('POST',route,body=src,headers={**API_HEADERS,'Authorization':'Bearer '+auth,
                             'Content-Type':'application/gzip','Content-Length':str(entry['bytes'])})
                response=conn.getresponse()
                status=response.status
                result=json.loads(response.read())
            conn.close()
            if status==201:
                require(result['size']==entry['bytes'] and result['state']=='uploaded' and
                        result.get('digest') in (None,'sha256:'+entry['sha256']),'incomplete upload '+city)
                print('Uploaded',city,entry['bytes'],flush=True)
                return
            if status==422:
                current=asset_list(auth,release).get(asset.name,{})
                if current.get('state')=='uploaded' and current.get('size')==entry['bytes'] and current.get('digest') in (None,'sha256:'+entry['sha256']):return
            raise IOError('GitHub asset upload returned '+str(status))
        except (OSError,http.client.HTTPException) as exc:
            if attempt==3:
                raise
            print('Retry',city,type(exc).__name__,flush=True)
            time.sleep(2**(attempt+1))

def render_report(packages,credits,gaps):
    lines=['# Offline bus coverage','','These counts describe the packs currently served to the app. Each stop uses operator-provided coordinates. Direction is inferred from neighbouring stops where a consistent direction exists. A frequency-based estimate represents an interval in the source schedule.','',
           'The United States catalog starts with the FTA National Transit Database’s published urban-area bus feeds and dated operator updates. Route inclusion uses a 35 km centre envelope extended to cover the official US Census TIGERweb municipal extents. The full routes that enter this area are retained. Great Britain uses the Department for Transport’s BODS/Traveline GTFS and official urban boundaries. Northern Ireland uses the National Transport Authority/Translink feed. Japan uses the open feeds credited for each city. A city listing does not guarantee every operator or service in that city.','',
           'Dates are local to the transit city. The end date is the last included dated service. Some operators finish earlier; the app can check for updates before the first expiring timetable.','',
           '| City ID | Routes | Boarding stops | Through |','|---|---:|---:|---|']
    for city in sorted(packages):
        e=packages[city]
        lines.append(f"| {city} | {e['routes']:,} | {e['stops']:,} | {e['validThrough']} |")
    lines+=['','## Known gaps','']
    for row in gaps['notes']:lines.append('- '+row)
    current={city:list(v) for city,v in gaps.get('cities',{}).items()}
    for city,entry in packages.items():
        for operator in entry.get('unavailableOperators',[]):
            current.setdefault(city,[]).append(operator+' (feed unavailable during latest refresh)')
    for city,items in sorted(current.items()):
        if items:lines.append('- **'+city+'** — '+'; '.join(sorted(set(items)))+'.')
    lines+=['','## Source credits','','Operators retain their respective rights. Raw GTFS is adapted into indexed offline calendars, route/stop relationships, boarding directions and scheduled stop times. The app does not represent these operators.']
    def esc(text):return str(text).replace('\n',' ').replace('[','\\[').replace(']','\\]')
    for city in sorted(credits):
        lines+=['','### '+city,'']
        for s in sorted(credits[city],key=lambda v:v['operator']):
            suffix=' '+esc(s['notice']) if s.get('notice') else ''
            terms=' [Operator terms](<'+s['terms']+'>).' if s.get('terms') else ''
            lines.append('- '+esc(s['operator'])+'. [Source](<'+s['source']+'>). [License](<'+s['license']+'>).'+suffix+terms)
    return '\n'.join(lines)+'\n'

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(data)
    os.replace(temp,path)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',type=Path,nargs='*')
    p.add_argument('--retire-old-assets',action='store_true',help='Remove only this workflow’s orphaned, obsolete bus assets after the manifest has been published')
    p.add_argument('--output',type=Path,default=ROOT/'packages/bus')
    p.add_argument('--upload',action='store_true',help='Upload and verify release assets before writing manifest')
    a=p.parse_args()
    if a.retire_old_assets:
        # Read the current remote manifest, not a stale local checkout, before touching release assets.
        url='https://raw.githubusercontent.com/'+REPO+'/main/packages/bus/manifest.json'
        with urllib.request.urlopen(url,timeout=40) as res:
            entries=json.load(res)['cities']
        require(entries and len(entries)>0,'empty published manifest')
        referenced={Path(e['path']).name for e in entries.values()}
        auth=token()
        release=release_info(auth,False)
        cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=5)
        for name,asset in asset_list(auth,release).items():
            if name not in referenced and re.fullmatch(r'[a-zA-Z0-9]+-[0-9a-f]{16}\.sqlite\.gz',name):
                created=dt.datetime.fromisoformat(asset['created_at'].replace('Z','+00:00'))
                if created<cutoff:
                    api('/releases/assets/'+str(asset['id']),auth,'DELETE')
        return
    files={}
    for stage in a.stage:
        found={f.name.removesuffix('.entry.json'):f for f in stage.glob('*.entry.json')}
        require(not set(found).intersection(files),'duplicate staged city')
        files.update(found)
    require(files,'no validated candidates')
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        packs=list(pool.map(lambda kv:validate(*kv),sorted(files.items())))
    print('Validated',len(packs),'city packs',flush=True)
    if a.upload:
        auth=token()
        release=release_info(auth,True)
        known=asset_list(auth,release)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda x:put_asset(x,auth,release,known),packs))
        final=asset_list(auth,release)
        for city,e,asset,_ in packs:
            remote=final.get(asset.name,{})
            require(remote.get('state')=='uploaded' and remote['size']==e['bytes'] and remote.get('digest') in (None,'sha256:'+e['sha256']),'missing remote asset '+city)
    manifest=a.output/'manifest.json'
    entries=json.loads(manifest.read_text())['cities'] if manifest.exists() else {}
    creditsfile=a.output/'sources.json'
    credits=json.loads(creditsfile.read_text()) if creditsfile.exists() else {}
    for city,entry,_,sources in packs:
        entries[city]=entry
        credits[city]=sources
    gaps=json.loads((ROOT/'inputs/bus-coverage-gaps.json').read_text())
    atomic(creditsfile,json.dumps(credits,sort_keys=True,ensure_ascii=False,indent=2)+'\n')
    atomic(a.output/'COVERAGE.md',render_report(entries,credits,gaps))
    atomic(manifest,json.dumps({'schemaVersion':1,'cities':entries},sort_keys=True,indent=2)+'\n')

if __name__=='__main__':
    main()
