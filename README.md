# opk

stream player for opk (Opus packet pack) audio using WebCodecs `AudioDecoder`

## Install

```sh
bun add github:nukoyaki/opk
```

this package ships TS source, so it's meant to be consumed through a bundler (esbuild, Vite, etc.)

## Usage

import with

```ts
import { OPKPlayer, OPKSampleRegionPlayer } from "opk";
```

you should use OPKPlayer for streaming and if you're instead using the opk to store a table of sound effects you use OPKSampleRegionPlayer<br/>

for packing the opk use opk.py:<br/>

```bash
# --loop-start and --loop-end can be omitted for waves that include the loop point
python3 opk.py --loop-start start --loop-end end input.wav output.opk
```

opk.py calls into ffmpeg, but you should still only work with wavs if you intend to use loop points as opk.py can automatically uses the loop points from the smpl chunk<br/>
the loop points are sample based rather then seconds, if you don't already have these values you can find them in <br/>audacity and set your selection like this,<br/>

first make sure both of the counters are set to samples<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-33.png>)<br/>
then click the gear and select start and end of selection<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-56.png>)<br/>