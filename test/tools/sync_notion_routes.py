#!/usr/bin/env python3
"""노션 루트 DB -> gamja_routes.json 동기화 (테스트판, v2: 노션 행 ID 키 + 라인 컬럼).

구조 (읽기 계층만 교체 가능):
  1) 읽기 계층  rows = [{key, num, name, vgrade, fa, sector, boulder, url, line}]
       A) read_rows_cdp()  : 로그인된 크롬(CDP 9222)의 노션 내부 API  (지금 사용)
       B) read_rows_api()  : 공식 Notion API (NOTION_TOKEN 환경변수, GitHub Actions 용)
       C) --rows 파일       : 테스트용
  2) transform()    : 순수 함수. rows + 이전 routes.json (+지오메트리) -> 새 routes.json + 보고서
  3) 안전장치(guard) : 행 0개 또는 루트가 30% 초과 감소하면 업로드/저장 없이 중단
  4) upload_if_changed(): `updated` 를 제외하고 달라졌을 때만 GitHub 업로드

routes.json 의 루트 키(key) = 노션 행(페이지) ID. thetopo URL 은 참고 정보일 뿐 키가 아님.
라인 컬럼 형식:  <photoId>:x,y;x,y;... [ | <photoId>:x,y;...]   (0~1 정규화, 시작->탑 순서)

사용 예:
  python3 -I sync_notion_routes.py --source cdp [--upload] [--dry] [--force]
  NOTION_TOKEN=... python3 sync_notion_routes.py --source api --database-id <id> --routes-file test/gamja_routes.json
토큰은 출력하지 않는다.
"""
import argparse, base64, copy, json, os, re, subprocess, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.dirname(HERE)
WS = os.path.dirname(V2)
KST = timezone(timedelta(hours=9))
REPO = 'kwangjae930807-lab/bouldermap'
REMOTE_PATH = 'test/gamja_routes.json'
TOKEN_FILE = os.path.join(WS, 'secrets', 'github_token')
SPACE = 'bb493c31-4f14-811b-bcda-0003464f0d40'

# 노션 내부 속성 키 (CDP 계층)
PK = dict(num='WSAG', name='title', vgrade='vgr', fa='fa_', sector='sec', boulder='bld', url='url', line='ln_')
# 공식 API 속성 이름
API_NAMES = dict(num='# 번호', name='루트', vgrade='V 그레이드', fa='초등', sector='섹터', boulder='볼더', url='thetopo', line='라인')

GUARD_MAX_REMOVED = 0.30


# ================================================================ 1) 읽기 계층
def _plain(prop):
    if not prop:
        return ''
    return ''.join(seg[0] for seg in prop if seg and isinstance(seg[0], str))


def rows_from_recordmap(rm):
    """queryCollection(v3) recordMap -> 표준 rows"""
    rows = []
    for bid, b in (rm.get('block') or {}).items():
        v = b.get('value', {})
        v = v.get('value', v)
        if v.get('parent_table') != 'collection' or not v.get('alive', True):
            continue
        p = v.get('properties', {})
        num = _plain(p.get(PK['num']))
        try:
            num = int(float(num)) if num else None
        except ValueError:
            num = None
        rows.append(dict(
            key=bid, num=num, name=_plain(p.get(PK['name'])).strip(),
            vgrade=_plain(p.get(PK['vgrade'])).strip(), fa=_plain(p.get(PK['fa'])).strip(),
            sector=_plain(p.get(PK['sector'])).strip(), boulder=_plain(p.get(PK['boulder'])).strip(),
            url=_plain(p.get(PK['url'])).strip(), line=_plain(p.get(PK['line'])).strip()))
    return rows


def read_rows_cdp(collection_id, view_id):
    js = ("(async()=>{const r=await fetch('/api/v3/queryCollection',{method:'POST',headers:{'Content-Type':'application/json','x-notion-space-id':'%s'},"
          "body:JSON.stringify({collection:{id:'%s',spaceId:'%s'},collectionView:{id:'%s',spaceId:'%s'},"
          "loader:{type:'reducer',reducers:{collection_group_results:{type:'results',limit:500}},searchQuery:'',userTimeZone:'Asia/Seoul'}})});"
          "if(r.status!==200)return {error:r.status};return await r.json()})()") % (SPACE, collection_id, SPACE, view_id, SPACE)
    jsf = os.path.join(HERE, 'js_query_rows.js')
    outf = os.path.join(HERE, 'query_rows_result.json')
    open(jsf, 'w').write(js)
    p = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'cdp_run.py'), jsf, '--out', outf],
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError('cdp helper failed: ' + p.stderr[-500:])
    res = json.load(open(outf))
    if not isinstance(res, dict) or 'recordMap' not in res:
        raise RuntimeError('queryCollection error: %s' % str(res)[:200])
    return rows_from_recordmap(res['recordMap'])


def _api_text(prop):
    if not prop:
        return ''
    t = prop.get('type')
    if t in ('title', 'rich_text'):
        return ''.join(x.get('plain_text', '') for x in prop.get(t) or [])
    if t == 'select':
        return (prop.get('select') or {}).get('name', '')
    if t == 'url':
        return prop.get('url') or ''
    return ''


def rows_from_api_results(results):
    """공식 API query 결과(results 배열) -> 표준 rows"""
    rows = []
    for pg in results:
        if pg.get('archived') or pg.get('in_trash'):
            continue
        pr = pg.get('properties', {})
        n = (pr.get(API_NAMES['num']) or {}).get('number')
        rows.append(dict(
            key=pg['id'], num=int(n) if n is not None else None,
            name=_api_text(pr.get(API_NAMES['name'])).strip(), vgrade=_api_text(pr.get(API_NAMES['vgrade'])).strip(),
            fa=_api_text(pr.get(API_NAMES['fa'])).strip(), sector=_api_text(pr.get(API_NAMES['sector'])).strip(),
            boulder=_api_text(pr.get(API_NAMES['boulder'])).strip(), url=_api_text(pr.get(API_NAMES['url'])).strip(),
            line=_api_text(pr.get(API_NAMES['line'])).strip()))
    return rows


def _diagnose(token):
    """404 시 통합이 볼 수 있는 객체(id/종류/제목)를 로그에 출력 (토큰 미출력)"""
    try:
        req = urllib.request.Request('https://api.notion.com/v1/search', method='POST',
                                     data=json.dumps({'page_size': 50}).encode(),
                                     headers={'Authorization': 'Bearer ' + token, 'Notion-Version': '2022-06-28',
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        print('DIAG accessible objects: %d' % len(d.get('results', [])))
        for o in d.get('results', []):
            t = o.get('title') or (o.get('properties', {}).get('title', {}) or {}).get('title') or []
            print('DIAG', o.get('object'), o.get('id'), ''.join(x.get('plain_text', '') for x in t)[:60],
                  'parent=%s' % json.dumps(o.get('parent'))[:120])
    except Exception as e:
        print('DIAG failed', repr(e)[:200])


def read_rows_api(database_id, token):
    """공식 Notion API (Notion-Version 2022-06-28), 페이지네이션 포함. 토큰은 출력하지 않는다."""
    results, cursor = [], None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        req = urllib.request.Request('https://api.notion.com/v1/databases/%s/query' % database_id, method='POST',
                                     data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + token, 'Notion-Version': '2022-06-28',
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _diagnose(token)
            raise RuntimeError('notion api http %s %s' % (e.code, e.read().decode('utf-8', 'replace')[:300]))
        results += d.get('results', [])
        if not d.get('has_more'):
            break
        cursor = d.get('next_cursor')
    return rows_from_api_results(results)


# ================================================================ 2) 변환 (순수 함수)
def url_keys(url):
    m = re.search(r'/routes/([^/?#]+)', url or '')
    if not m:
        return []
    seg = m.group(1)
    keys = [('slug', seg)]
    n = re.search(r'-(\d+)$', seg) or re.fullmatch(r'(\d+)', seg)
    if n:
        keys.insert(0, ('id', int(n.group(1))))
    return keys


def parse_lines(s):
    """라인 문자열 -> (lines, error).  lines=[{photoId, points:[[x,y],...]}]  error: None | 사유
    빈 문자열이면 ([], None). 형식 오류가 하나라도 있으면 (None, 사유)."""
    s = (s or '').strip()
    if not s:
        return [], None
    out = []
    for seg in s.split('|'):
        seg = seg.strip()
        if not seg:
            continue
        m = re.fullmatch(r'(\d+)\s*:\s*(.+)', seg, re.S)
        if not m:
            return None, '구간 형식 오류: %r' % seg[:30]
        pts = []
        for tok in m.group(2).split(';'):
            tok = tok.strip()
            if not tok:
                continue
            xy = tok.split(',')
            try:
                if len(xy) != 2:
                    raise ValueError
                x, y = float(xy[0]), float(xy[1])
            except ValueError:
                return None, '점 형식 오류: %r' % tok[:20]
            if not (-0.01 <= x <= 1.01 and -0.01 <= y <= 1.01):
                return None, '좌표 범위(0~1) 밖: %r' % tok[:20]
            pts.append([round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)])
        if len(pts) < 2:
            return None, '점이 2개 미만 (사진 %s)' % m.group(1)
        out.append(dict(photoId=int(m.group(1)), points=pts))
    return out, None


def photo_ids(geometry):
    ids = set()
    for b in (geometry or {}).get('boulders', []):
        for p in b.get('photos', []):
            ids.add(p.get('photoId'))
    return ids


FIELDS = ('num', 'name', 'vgrade', 'fa', 'boulder', 'thetopo_url', 'route_id', 'grade')


def transform(rows, prev, now_iso, geometry=None):
    """rows + prev(routes.json) -> (new routes.json, report). 부작용 없음.
    report = dict(added, removed, changed, warnings)"""
    by_key, by_rid, by_slug = {}, {}, {}
    for r in prev.get('routes', []):
        if r.get('key'):
            by_key[r['key']] = r
        if r.get('route_id') is not None:
            by_rid.setdefault(r['route_id'], r)
        for u in [r.get('thetopo_url')] + list(r.get('alt_urls') or []):
            for k in url_keys(u):
                if k[0] == 'slug':
                    by_slug.setdefault(k[1], r)
    gids = photo_ids(geometry) if geometry else None
    new = {k: v for k, v in prev.items() if k != 'routes'}
    out, seen, matched_prev = [], set(), set()
    rep = dict(added=[], removed=[], changed=[], warnings=[])
    for row in rows:
        key = row.get('key')
        label = '%s %s' % (row.get('num') if row.get('num') is not None else '-', row.get('name') or '(이름 없음)')
        if not key or key in seen:
            continue
        if not row.get('name') and row.get('num') is None:
            rep['warnings'].append('빈 행 건너뜀: %s' % key)
            continue
        seen.add(key)
        base = by_key.get(key)
        ks = url_keys(row.get('url'))
        rid = next((k[1] for k in ks if k[0] == 'id'), None)
        if base is None:      # 구버전(키 없는 routes.json) 이전용: URL 로 한 번만 대응
            base = next((by_rid[k[1]] for k in ks if k[0] == 'id' and k[1] in by_rid), None) or \
                   next((by_slug[k[1]] for k in ks if k[0] == 'slug' and k[1] in by_slug), None)
            if base is not None and base.get('key'):
                base = None
        if base is not None:
            matched_prev.add(id(base))
        if rid is None and base is not None:
            rid = base.get('route_id')
        lines, err = parse_lines(row.get('line'))
        if err:
            lines = copy.deepcopy(base.get('lines', [])) if base else []
            rep['warnings'].append('라인 형식 오류, 이전 값 유지: %s (%s)' % (label, err))
        elif not lines:
            rep['warnings'].append('라인 없음: %s' % label)
        elif gids is not None:
            for ln in lines:
                if ln['photoId'] not in gids:
                    rep['warnings'].append('알 수 없는 사진 ID %s: %s' % (ln['photoId'], label))
        r = dict(key=key, route_id=rid, num=row.get('num'), name=row.get('name', ''), vgrade=row.get('vgrade', ''),
                 grade=(base or {}).get('grade', ''), fa=row.get('fa', ''), boulder=row.get('boulder', ''),
                 thetopo_url=row.get('url', ''), lines=lines)
        out.append(r)
        if base is None:
            rep['added'].append(label)
        else:
            diff = {f: [base.get(f), r.get(f)] for f in FIELDS + ('fa',) if base.get(f) != r.get(f)}
            if (base.get('lines') or []) != lines:
                diff['lines'] = [sum(len(l['points']) for l in base.get('lines') or []), sum(len(l['points']) for l in lines)]
            if diff:
                rep['changed'].append(dict(row=label, fields=diff))
    for p in prev.get('routes', []):
        if (p.get('key') in seen) or id(p) in matched_prev:
            continue
        rep['removed'].append('%s %s' % (p.get('num'), p.get('name')))
    out.sort(key=lambda r: (r.get('num') is None, r.get('num') or 0))
    new['routes'] = out
    new['updated'] = now_iso
    new['source'] = 'notion'
    return new, rep


def guard(rows, prev, new):
    """오류 문자열 또는 None. 행 0개 / 루트 30% 초과 감소 시 중단 사유 반환."""
    if not rows:
        return '노션에서 읽은 행이 0개 (API/권한 장애 의심)'
    n_prev = len(prev.get('routes', []))
    if n_prev and len(new['routes']) < n_prev * (1 - GUARD_MAX_REMOVED):
        return '루트가 %d -> %d 개로 %d%% 초과 감소 (장애 의심, --force 로 무시 가능)' % (
            n_prev, len(new['routes']), int(GUARD_MAX_REMOVED * 100))
    return None


def dumps_compact(d):
    """indent=1 JSON, 점 좌표 [x, y] 는 한 줄로"""
    s = json.dumps(d, ensure_ascii=False, indent=1)
    return re.sub(r'\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]', r'[\1, \2]', s)


def canonical(d):
    d = {k: v for k, v in d.items() if k != 'updated'}
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


# ================================================================ 3) 업로드
def _token():
    return open(TOKEN_FILE).read().strip()


def _gh(method, path, body=None):
    req = urllib.request.Request('https://api.github.com/repos/%s/%s' % (REPO, path), method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={'Authorization': 'Bearer ' + _token(), 'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'bouldermap-sync'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def upload_if_changed(new, message):
    cur = _gh('GET', 'contents/' + REMOTE_PATH + '?ref=main')
    remote = json.loads(base64.b64decode(cur['content']).decode('utf-8'))
    if canonical(remote) == canonical(new):
        return 'unchanged'
    body = dumps_compact(new).encode('utf-8')
    _gh('PUT', 'contents/' + REMOTE_PATH, {'message': message, 'branch': 'main', 'sha': cur['sha'],
                                           'content': base64.b64encode(body).decode()})
    return 'uploaded'


# ================================================================ main
def log_write(path, entry):
    if path:
        with open(path, 'a', encoding='utf-8') as lf:
            lf.write(json.dumps(entry, ensure_ascii=False) + '\n')


def main():
    ids_file = os.path.join(V2, 'test_page_ids.json')
    ids = json.load(open(ids_file)) if os.path.exists(ids_file) else {}
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['cdp', 'api', 'rows'], default=os.environ.get('SYNC_SOURCE', 'cdp'))
    ap.add_argument('--collection', default=ids.get('coll'))
    ap.add_argument('--view', default=ids.get('view'))
    ap.add_argument('--database-id', default=os.environ.get('NOTION_DATABASE_ID') or ids.get('collBlock'))
    ap.add_argument('--rows', help='rows.json (--source rows)')
    ap.add_argument('--routes-file', default=os.path.join(V2, 'test', 'gamja_routes.json'))
    ap.add_argument('--geometry-file', default=os.path.join(V2, 'test', 'gamja_topo_geometry.json'))
    ap.add_argument('--log-file', default=os.path.join(V2, 'sync_log.jsonl'))
    ap.add_argument('--upload', action='store_true')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()

    now = datetime.now(KST).isoformat(timespec='seconds')
    if a.source == 'rows':
        rows = json.load(open(a.rows))
    elif a.source == 'cdp':
        rows = read_rows_cdp(a.collection, a.view)
    else:
        tok = os.environ.get('NOTION_TOKEN')
        if not tok:
            sys.exit('NOTION_TOKEN 환경변수 필요')
        rows = read_rows_api(a.database_id, tok)
    prev = json.load(open(a.routes_file)) if os.path.exists(a.routes_file) else {'routes': []}
    geometry = json.load(open(a.geometry_file)) if os.path.exists(a.geometry_file) else None
    new, rep = transform(rows, prev, now, geometry)
    print('rows=%d routes=%d added=%d removed=%d changed=%d warnings=%d' % (
        len(rows), len(new['routes']), len(rep['added']), len(rep['removed']), len(rep['changed']), len(rep['warnings'])))
    for k in ('added', 'removed'):
        for x in rep[k]:
            print('  %s: %s' % (k, x))
    for c in rep['changed']:
        print('  changed: %s %s' % (c['row'], json.dumps(c['fields'], ensure_ascii=False)[:200]))
    for w in rep['warnings']:
        print('  WARN:', w)
    err = None if a.force else guard(rows, prev, new)
    if err:
        print('ERROR(중단, 저장/업로드 안 함):', err)
        log_write(a.log_file, dict(at=now, source=a.source, status='aborted', error=err, rows=len(rows), routes_prev=len(prev.get('routes', []))))
        sys.exit(2)
    changed = canonical(prev) != canonical(new)
    print('changed vs local file:', changed)
    log_write(a.log_file, dict(at=now, source=a.source, status='dry' if a.dry else 'ok', rows=len(rows), routes=len(new['routes']),
                               file_changed=changed, **rep))
    if a.dry:
        return
    if changed:
        open(a.routes_file, 'w', encoding='utf-8').write(dumps_compact(new))
    if a.upload:
        print('github:', upload_if_changed(new, 'test: sync gamja routes from notion'))


if __name__ == '__main__':
    main()
