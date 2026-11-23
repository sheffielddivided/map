# map
Aker BP map

## NCS Mapmaker har flyttet

Mapmaker (tidligere `ppt.html` her) ligger nå i et eget repo,
[`sheffielddivided/mapmaker`](https://github.com/sheffielddivided/mapmaker), og er publisert på
https://map.kbak.no/. `ppt.html` sender videre dit og tar med oppsettet i lenken (`#s=…`), så gamle
lenker og bokmerker fortsatt virker. Datahentingen til Mapmaker (`fetch_data.py`) og kartlagene i
`data/` flyttet med.

## Data for de interaktive kartene (`index.html`, `embed.html`, `builder.html`)

`fetch_interactive.py` henter Aker BPs lisenser, samordnede områder (BAA), felt og funn med
eierandeler fra Sodir, Aker BPs britiske lisenser fra NSTA, samt Sodirs felt- og funnbeskrivelser,
og skriver dem til `data/akerbp/*.json`. Sidene leser bare disse filene, så de sender ingen
spørringer til Sodir eller NSTA. Skriptet kjøres daglig av `.github/workflows/update-map-data.yml` (dataene er dermed
opptil ett døgn gamle), beholder forrige fil hvis en kilde feiler, og kan kjøres lokalt:

```
python fetch_interactive.py
```

Sidene må serveres over HTTP(S), f.eks. via GitHub Pages eller lokalt med
`python -m http.server`. De fungerer ikke åpnet direkte som fil.
