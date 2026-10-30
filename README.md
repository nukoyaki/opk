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

you should use OPKPlayer for streaming and if you're instead using the opk to store a table of sound effects you use OPKSampleRegionPlayer, however the header has no native support to store the sound regions so you must hardcode the regions in your uses of OPKSampleRegionPlayer, also OPKSampleRegionPlayer doesn't stream the regions when requested, it will load the entire audio instead<br/>

for packing the opk use opus_pack.py:<br/>

```bash
python3 opus_pack.py --loop-start start --lop-end end input.ogg output.opk
```

opus_pack.py assumes the input is already an ogg container with opus audio (use ffmpeg for that)<br/>
the loop points are sample based rather then seconds, if you don't already have these values you can find them in <br/>audacity and set your selection like this,<br/>

first make sure both of the counters are set to samples<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-33.png>)<br/>
then click the gear and select start and end of selection<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-56.png>)<br/>