"""
Henter dataene til de interaktive Aker BP-kartene (index.html, embed.html og
builder.html) og lagrer dem under data/akerbp/, slik at sidene leser noen få
statiske filer i stedet for å sende 20-30 spørringer til Sodir og NSTA ved hver
sidevisning.

Filene har samme egenskaper som sidene tidligere bygget i nettleseren
(Sodir-feltnavn som prlName, fldNpdidField osv., pluss isOperator, _myShare og
_allLicensees), så visningskoden er uendret.

    licences.json         Aker BPs lisenser (operatør eller rettighetshaver)
    units.json            Aker BPs samordnede områder (BAA)
    fields.json           Aker BPs felt
    other_fields.json     Øvrige felt på norsk sokkel som ikke er stengt
    discoveries.json      Alle aktuelle funn, med _other=true for funn uten Aker BP
    uk_licences.json      Britiske lisenser der Aker BP er rettighetshaver (NSTA)
    field_texts.json      Sodirs «Development»- og «Status»-tekster per felt
    discovery_texts.json  Sodirs beskrivelse per funn (engelsk, ellers norsk)

Kjøres daglig av .github/workflows/update-map-data.yml sammen med
fetch_data.py, og kan kjøres lokalt (kun standardbiblioteket):

    python fetch_interactive.py

Utdata er deterministisk, så filene bare endrer seg når dataene endrer seg. Feiler
en kilde, eller gir den et tomt svar, beholdes filen fra forrige kjøring.
"""

import json
import os
import sys
import urllib.parse

from fetch_data import BASE, _get

AKERBP_ID = 28544099
# NSTA sitt organisasjonsnummer for Aker BP.
AKERBP_NSTA = '12908310'
NSTA_LIC = ('https://services-eu1.arcgis.com/OZMfUznmLTnWccBc/arcgis/rest/services/'
            + urllib.parse.quote('UKCS offshore petroleum licences WGS84') + '/FeatureServer/0')
OUT_DIR = os.path.join('data', 'akerbp')
PAGE_SIZE = 1000
# Lett forenkling (ca. 5 m) og 5 desimaler (ca. 1 m): ser lik ut som rådata i kartet,
# men gir vesentlig mindre filer.
SIMPLIFY = 0.00005
PRECISION = 5

FIELD_STATUS_EXCLUDED = ('Shut down', 'Abandoned', 'Returned to authorities')
DISC_STATUS_EXCLUDED = ('Included in other discovery', 'Producing', 'Shut down')


def sql_list(values):
    return ','.join("'" + v.replace("'", "''") + "'" for v in values)


def query(layer, where, fields, geometry=False):
    """Alle rader fra et Sodir-lag (lagnummer) eller en full lag-URL, paginert på OBJECTID.
    Returnerer GeoJSON-features ({'properties', 'geometry'})."""
    url = layer if isinstance(layer, str) else f'{BASE}/{layer}'
    rows, offset = [], 0
    while True:
        params = {
            'where': where,
            'outFields': ','.join(fields),
            'orderByFields': 'OBJECTID',
            'resultOffset': offset,
            'resultRecordCount': PAGE_SIZE,
            'returnGeometry': 'true' if geometry else 'false',
            'f': 'geojson' if geometry else 'json',
        }
        if geometry:
            params.update(outSR=4326, maxAllowableOffset=SIMPLIFY, geometryPrecision=PRECISION)
        data = _get(f'{url}/query', params)
        page = data.get('features', [])
        if not geometry:
            page = [{'properties': f['attributes']} for f in page]
        rows.extend(page)
        more = data.get('exceededTransferLimit') or (data.get('properties') or {}).get('exceededTransferLimit')
        if not page or not more:
            break
        offset += len(page)
    print(f'  {str(layer).split("/services/")[-1]} [{where[:60]}]: {len(rows)} rader')
    return rows


def query_in(layer, key, ids, fields, geometry=False, batch=100):
    """Som query(), men for en liste id-er (delt opp så URL-en ikke blir for lang)."""
    ids = sorted(ids)
    rows = []
    for i in range(0, len(ids), batch):
        part = ','.join(str(x) for x in ids[i:i + batch])
        rows += query(layer, f'{key} IN ({part})', fields, geometry)
    return rows


def attrs(rows):
    return [r['properties'] for r in rows]


def group(rows, key):
    out = {}
    for a in rows:
        out.setdefault(a[key], []).append(a)
    return out


def sort_shares(items):
    return sorted(items, key=lambda x: (-(x.get('share') or 0), x.get('company') or ''))


def collection(features, key):
    """Dedupliserte, sorterte features. Features uten geometri får forrige kjente
    geometri (Sodir sender av og til features uten geometri)."""
    seen, out = set(), []
    for f in features:
        fid = f['properties'].get(key)
        if fid in seen:
            continue
        seen.add(fid)
        out.append({'type': 'Feature', 'geometry': f.get('geometry'), 'properties': f['properties']})
    out.sort(key=lambda f: str(f['properties'].get(key)))
    return out


# ── Lisenser ─────────────────────────────────────────────────────────────────
def build_licences():
    oper = {a['prlNpdidLicence'] for a in attrs(query(
        3008, f'cmpNpdidCompany = {AKERBP_ID} AND prlOperDateValidTo IS NULL', ['prlNpdidLicence']))}
    current = attrs(query(3007, 'prlLicenseeDateValidTo IS NULL',
                          ['prlNpdidLicence', 'cmpNpdidCompany', 'cmpLongName', 'prlLicenseeInterest', 'prlLicenseeSdfi']))
    mine = {a['prlNpdidLicence']: a['prlLicenseeInterest'] for a in current if a['cmpNpdidCompany'] == AKERBP_ID}
    ids = oper | set(mine)
    licensees = group([a for a in current if a['prlNpdidLicence'] in ids], 'prlNpdidLicence')
    rows = query_in(3000, 'prlNpdidLicence', ids,
                    ['prlNpdidLicence', 'prlName', 'prlMainArea', 'prlStatus', 'prlPhaseCurrent',
                     'prlCurrentArea', 'prlFactPageUrl'], geometry=True)
    for r in rows:
        p = r['properties']
        lid = p['prlNpdidLicence']
        p['isOperator'] = lid in oper
        p['_myShare'] = mine.get(lid)
        p['_allLicensees'] = sort_shares({'company': a['cmpLongName'], 'share': a['prlLicenseeInterest'],
                                          'sdfi': a['prlLicenseeSdfi']} for a in licensees.get(lid, []))
    return collection(rows, 'prlNpdidLicence')


# ── Samordnede områder (BAA) ─────────────────────────────────────────────────
def build_units():
    oper = {a['baaNpdidBsnsArrArea'] for a in attrs(query(
        3302, f'cmpNpdidCompany = {AKERBP_ID} AND baaOperatorDateValidTo IS NULL', ['baaNpdidBsnsArrArea']))}
    current = attrs(query(3304, 'baaLicenseeDateValidTo IS NULL',
                          ['baaNpdidBsnsArrArea', 'cmpNpdidCompany', 'cmpLongName', 'baaLicenseeInterest', 'baaLicenseeSdfi']))
    mine = {a['baaNpdidBsnsArrArea']: a['baaLicenseeInterest'] for a in current if a['cmpNpdidCompany'] == AKERBP_ID}
    ids = oper | set(mine)
    licensees = group([a for a in current if a['baaNpdidBsnsArrArea'] in ids], 'baaNpdidBsnsArrArea')
    geo_fields = ['baaNpdidBsnsArrArea', 'baaName', 'baaKind', 'baaActive', 'baaFactPageUrl']
    # Som før: områdene Aker BP opererer (cmpNpdidCompany i lag 3300), pluss dem der Aker BP er partner.
    rows = query(3300, f'cmpNpdidCompany = {AKERBP_ID}', geo_fields, geometry=True)
    have = {r['properties']['baaNpdidBsnsArrArea'] for r in rows}
    rows += query_in(3300, 'baaNpdidBsnsArrArea', ids - have, geo_fields, geometry=True)
    for r in rows:
        p = r['properties']
        uid = p['baaNpdidBsnsArrArea']
        p['isOperator'] = uid in oper
        p['_myShare'] = mine.get(uid)
        p['_allLicensees'] = sort_shares({'company': a['cmpLongName'], 'share': a['baaLicenseeInterest'],
                                          'sdfi': a['baaLicenseeSdfi']} for a in licensees.get(uid, []))
    return collection(rows, 'baaNpdidBsnsArrArea')


# ── Felt ─────────────────────────────────────────────────────────────────────
FIELD_GEO = ['fldNpdidField', 'fldName', 'fldMainArea', 'fldCurrentActivitySatus', 'fldHcType',
             'fldDiscoveryYear', 'fldOwnerName', 'fldFactPageUrl']


def build_fields():
    oper = {a['fldNpdidField'] for a in attrs(query(
        7110, f'cmpNpdidCompany = {AKERBP_ID} AND fldOperatorTo IS NULL', ['fldNpdidField']))}
    current = attrs(query(7108, 'fldLicenseeTo IS NULL',
                          ['fldNpdidField', 'cmpNpdidCompany', 'cmpLongName', 'fldCompanyShare',
                           'fldOwnerKind', 'fldOwnerName', 'fldSdfiShare']))
    mine = {}
    for a in current:
        if a['cmpNpdidCompany'] != AKERBP_ID:
            continue
        m = mine.setdefault(a['fldNpdidField'], {'total': 0, 'owners': []})
        m['total'] += float(a['fldCompanyShare'] or 0)
        m['owners'].append({'kind': a['fldOwnerKind'], 'name': a['fldOwnerName'], 'share': a['fldCompanyShare']})
    for m in mine.values():
        m['total'] = round(m['total'], 6)
        m['owners'].sort(key=lambda o: (o['kind'] or '', o['name'] or ''))
    ids = oper | set(mine)
    licensees = group([a for a in current if a['fldNpdidField'] in ids], 'fldNpdidField')
    rows = query_in(7100, 'fldNpdidField', ids, FIELD_GEO, geometry=True)
    for r in rows:
        p = r['properties']
        fid = p['fldNpdidField']
        p['isOperator'] = fid in oper
        p['_myShare'] = mine.get(fid)
        p['_allLicensees'] = sort_shares({'company': a['cmpLongName'], 'share': a['fldCompanyShare'],
                                          'ownerKind': a['fldOwnerKind'], 'ownerName': a['fldOwnerName'],
                                          'sdfi': a['fldSdfiShare']} for a in licensees.get(fid, []))
    return collection(rows, 'fldNpdidField')


def build_other_fields(akerbp_ids):
    rows = query(7100, f'fldCurrentActivitySatus NOT IN ({sql_list(FIELD_STATUS_EXCLUDED)})',
                 [f for f in FIELD_GEO if f != 'fldOwnerName'], geometry=True)
    return collection([r for r in rows if r['properties']['fldNpdidField'] not in akerbp_ids], 'fldNpdidField')


# ── Funn ─────────────────────────────────────────────────────────────────────
def build_discoveries():
    rows = query(7000, f'dscCurrentActivityStatus NOT IN ({sql_list(DISC_STATUS_EXCLUDED)})',
                 ['dscNpdidDiscovery', 'dscName', 'dscCurrentActivityStatus', 'dscFactPageUrl', 'dscDiscoveryYear',
                  'dscHcType', 'cmpLongName', 'cmpNpdidCompany', 'nmaName', 'fldName', 'fldNpdidField'], geometry=True)
    mine = {a['dscNpdidDiscovery'] for a in attrs(query(7003, f'cmpNpdidCompany = {AKERBP_ID}', ['dscNpdidDiscovery']))}
    for r in rows:
        p = r['properties']
        p['_other'] = p['dscNpdidDiscovery'] not in mine
        p['_isDiscovery'] = True
    return collection(rows, 'dscNpdidDiscovery')


# ── Britiske lisenser ────────────────────────────────────────────────────────
def build_uk_licences():
    rows = query(NSTA_LIC, f"ADMORGNO='{AKERBP_NSTA}' OR LICORG LIKE '%{AKERBP_NSTA}%'",
                 ['LICREF', 'LICNO', 'LICPREFIX', 'LICTYPE', 'LICSTATUS', 'LICORG', 'ADMORG', 'ADMORGNO', 'BLOCKREF',
                  'FIELDS', 'LICSTARTDT', 'LICENDDT', 'RNDNO', 'RNDTYPE', 'REGIONS', 'URL'], geometry=True)
    for r in rows:
        r['properties'].pop('OBJECTID', None)
    return collection(rows, 'LICREF')


# ── Beskrivelser (vises når man klikker på et felt eller funn) ───────────────
def build_field_texts(ids):
    rows = attrs(query(7102, "fldDescriptionHeading IN ('Development','Status')",
                       ['fldNpdidField', 'fldDescriptionHeading', 'fldDescriptionText']))
    out = {}
    for a in rows:
        if a['fldNpdidField'] not in ids or not a['fldDescriptionText']:
            continue
        key = 'dev' if a['fldDescriptionHeading'] == 'Development' else 'sta'
        out.setdefault(str(a['fldNpdidField']), {})[key] = a['fldDescriptionText']
    return out


def build_discovery_texts(ids):
    rows = attrs(query(7001, '1=1', ['dscNpdidDiscovery', 'dscDescriptionText', 'dscCultureCode']))
    by = {}
    for a in rows:
        if a['dscNpdidDiscovery'] in ids and a['dscDescriptionText']:
            by.setdefault(a['dscNpdidDiscovery'], {})[a['dscCultureCode']] = a['dscDescriptionText']
    return {str(k): v.get('en') or v.get('no') for k, v in by.items() if v.get('en') or v.get('no')}


# ── Skriving ─────────────────────────────────────────────────────────────────
def path_of(name):
    return os.path.join(OUT_DIR, f'{name}.json')


def load_previous(name):
    try:
        with open(path_of(name), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def fill_missing_geometry(name, features, key):
    prev = load_previous(name) or {}
    old = {f['properties'].get(key): f.get('geometry') for f in prev.get('features', [])}
    for f in features:
        if not f.get('geometry') and old.get(f['properties'].get(key)):
            print(f'    ! mangler geometri fra kilden, bruker forrige: {name}/{f["properties"].get(key)}')
            f['geometry'] = old[f['properties'].get(key)]
    return [f for f in features if f.get('geometry')]


def write(name, data):
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    try:
        with open(path_of(name), encoding='utf-8') as fh:
            if fh.read() == body:
                print(f'  {path_of(name)}: uendret')
                return
    except OSError:
        pass
    with open(path_of(name), 'w', encoding='utf-8') as fh:
        fh.write(body)
    print(f'  {path_of(name)}: skrevet ({len(body) // 1024} kB)')


def run(name, build, key=None):
    """Bygger og skriver én fil. Ved feil eller tomt svar beholdes forrige fil."""
    print(f'Henter {name} ...')
    try:
        data = build()
    except Exception as exc:  # noqa: BLE001 - én kilde som feiler skal ikke stoppe de andre
        print(f'  ! {name} feilet: {exc} - beholder forrige fil', file=sys.stderr)
        return load_previous(name)
    if key:
        data = {'type': 'FeatureCollection', 'features': fill_missing_geometry(name, data, key)}
        if not data['features']:
            print(f'  ! ingen features for {name} - beholder forrige fil', file=sys.stderr)
            return load_previous(name)
    elif not data:
        print(f'  ! tomt svar for {name} - beholder forrige fil', file=sys.stderr)
        return load_previous(name)
    write(name, data)
    return data


def ids_of(data, key):
    return {f['properties'][key] for f in (data or {}).get('features', [])}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    run('licences', build_licences, 'prlNpdidLicence')
    run('units', build_units, 'baaNpdidBsnsArrArea')
    fields = run('fields', build_fields, 'fldNpdidField')
    akerbp_fields = ids_of(fields, 'fldNpdidField')
    other = run('other_fields', lambda: build_other_fields(akerbp_fields), 'fldNpdidField')
    discs = run('discoveries', build_discoveries, 'dscNpdidDiscovery')
    run('uk_licences', build_uk_licences, 'LICREF')
    run('field_texts', lambda: build_field_texts(akerbp_fields | ids_of(other, 'fldNpdidField')))
    run('discovery_texts', lambda: build_discovery_texts(ids_of(discs, 'dscNpdidDiscovery')))


if __name__ == '__main__':
    main()
