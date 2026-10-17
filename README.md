# map
Aker BP map

## PPT-kart (`ppt.html`)

Lager egendefinerte kart over norsk, britisk og dansk sokkel til PowerPoint-presentasjoner.

- **Kartlag:** felt i produksjon, felt under utbygging, nedstengte felt, funn, aktive lisenser,
  transportrørledninger, innretninger og letebrønner (filtrert på år, formål og resultat).
- **Sektorer:** norsk, britisk og dansk sektor kan slås av og på hver for seg, med sektorgrenser.
- **Selskapsfilter:** velg ett eller flere selskaper som operatør og/eller partner. De kan fremheves
  (andre vises i grått) eller vises alene, med valgfri eierandel i etiketten. Gjelder felt, funn og lisenser.
- **Bakgrunn:** vektorkart med kystlinje, dybdekurver, landegrenser, valg av hvilke land som vises (f.eks. bare Norge og Danmark), byer og kvadrant-/blokknett (kvadranter med eller uten nummer).
  Fire fargetemaer pluss egne farger, og transparent bakgrunn.
- **Slide-format:** 16:9, halvside, 4:3, kvadrat, stående eller egendefinert. Rammen viser nøyaktig
  det som eksporteres.
- **Eksport:** *Kopier* legger en PNG på utklippstavlen (lim inn i PowerPoint med Ctrl+V), *PNG*
  laster ned i opptil 8K, og *SVG* gir vektorgrafikk. I PowerPoint kan SVG-en konverteres til figur
  ved å høyreklikke og velge «Konverter til figur».
- **Etiketter:** overlappende etiketter skjules automatisk. Dra en etikett for å flytte den, og
  dobbeltklikk for å skjule den.
- **Språk:** norsk eller engelsk, både i grensesnittet og i kartet (tegnforklaring, kildehenvisning, verktøytips).
- **Lagring:** hele oppsettet ligger i URL-en, så kart kan bokmerkes og deles. Navngitte maler
  lagres lokalt i nettleseren.

### Data

`fetch_data.py` (kun Python-standardbiblioteket) henter alle lag og skriver `data/*.json`. Hver
feature har `cc` = `no`, `uk` eller `dk`.

| Sektor | Kilde | Lag |
| --- | --- | --- |
| Norge | [Sodir FactMaps](https://factmaps.sodir.no) (NLOD 2.0) | felt, funn, lisenser, letebrønner, rørledninger, innretninger, kvadranter, blokker |
| Storbritannia | [NSTA Open Data](https://opendata-nstauthority.hub.arcgis.com) (Open Government Licence v3.0) | felt (med rettighetshavere), uutbygde funn (punkter), lisens-delområder med eierandel, lete-/avgrensningsbrønner, transportrør ≥ 16", plattformer/FPSO/terminaler, havbunnsrammer, kvadranter, blokker |
| Danmark | Energistyrelsens data via [GEUS WFS](https://data.geus.dk/geusmap/ows/4326.jsp) (feltavgrensninger, rørledninger, brønnresultater) og shapefilene i [sheffielddivided/ens](https://github.com/sheffielddivided/ens) | felt (status fra års- og månedsproduksjonen, eierandeler fra `ownership.json`), rørledninger (gass/olje/flerfase), lisenser, letebrønner med resultat (til 2019), innretninger, blokker |

Selskapsnavn normaliseres til konsernnavn (`GROUPS` i skriptet), så ett valg dekker datterselskaper
i alle land. `data/companies.json` kobler juridiske navn til konsernnavn, slik at eldre lenker og
maler fortsatt virker.

GitHub Action-en `.github/workflows/update-map-data.yml` kjører skriptet daglig og committer bare
når dataene faktisk er endret. Feiler én kilde, beholdes forrige data for det landet. Skriptet kan
også kjøres lokalt:

```
python fetch_data.py
```

`data/basemap.json` (kystlinje, landegrenser, dybdekurver og sektorgrenser) er statisk. Den er bygget én gang fra
[Natural Earth](https://www.naturalearthdata.com) 10m (public domain) med
[mapshaper](https://github.com/mbloch/mapshaper):

```
mapshaper -i ne_10m_land.shp ne_10m_minor_islands.shp combine-files -merge-layers force \
  -clip bbox=-14,48,50,83 -dissolve2 -simplify 70% weighted keep-shapes \
  -filter-slivers min-area=0.3km2 -rename-layers land -o land.json format=topojson
# tilsvarende for admin_0_boundary_lines_land (borders, med -filter-fields ADM0_A3_L,ADM0_A3_R -rename-fields l=ADM0_A3_L,r=ADM0_A3_R) og bathymetry_K_200/J_1000/I_2000/H_3000
# (d200/d1000/d2000/d3000), deretter:
# landene hver for seg (for landvalget), fra admin_0_countries:
mapshaper -i ne_10m_admin_0_countries.shp -clip bbox=-14,48,50,83 -filter-fields ADM0_A3,NAME \
  -rename-fields c=ADM0_A3 -simplify 70% weighted keep-shapes -filter-slivers min-area=0.3km2 \
  -rename-layers countries -o countries.json format=topojson
mapshaper -i land.json borders.json d200.json d1000.json d2000.json d3000.json sectors.json countries.json \
  combine-files -o basemap.json format=topojson quantization=100000
```

Sektorgrensene (`sectors`) er de felles kantene mellom hvert lands blokknett (Sodir, NSTA og ENS),
laget med `mapshaper -i no.json uk.json dk.json combine-files snap -merge-layers force -dissolve2 cc -innerlines`.

Siden må serveres over HTTP(S), f.eks. via GitHub Pages eller lokalt med
`python -m http.server`. Den fungerer ikke åpnet direkte som fil.
