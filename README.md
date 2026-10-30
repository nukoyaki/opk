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
python3 opk.py --loop-start start --loop-end end --bitrate 128k input.wav output.opk
```

opk.py calls into ffmpeg so you need ffmpeg with libopus on the path, despite any input supported by ffmpeg being availabile you should still only work with wavs if you intend to use loop points as opk.py can automatically get the loop points from the smpl chunk<br/>
the loop points are sample based rather then seconds, if you don't already have these values you can find them in <br/>audacity and set your selection like this,<br/>

first make sure both of the counters are set to samples<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-33.png>)<br/>
then click the gear and select start and end of selection<br/>
![alt text](<.doc/Screenshot 2026-10-29 20-10-56.png>)<br/>

also make sure any audio you use is 48kHz as opus only does 48kHz, otherwise you will likely run into looping issues as that prevents loops from being sample accurate, additionally u8, s8, and f64 wavs are not implemented and will error (and the same goes for ADPCM obviously)<br/>

when packing for OPKSampleRegionPlayer you specify an input folder which contains multiple audios, since these won't do any looping you don't need to worry about them being wav files, though they should still be lossless though (flac/wv for example) as doing lossy to lossy compression is stupid and you should avoid that everywhere and not just with OPK<br/>

```bash
python3 opk.py --bitrate 128k ./inputs/ output.opk
```