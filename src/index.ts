const MAGIC = "OPKT";
const VERSION = 1;
const HEADER_SIZE = 28;
const RATE = 48000;

const FETCH_AHEAD = 1;
const FETCH_CHUNK = 2;
const FIRST_FETCH_BYTES = 8 * 1024;
const MIN_FETCH_BYTES = 8 * 1024;
const RETRY_MS = 2000;

const DECODE_AHEAD = 3;
const MAX_DECODE_QUEUE = 4;
const BLOCK_FRAMES = RATE / 4;
const PREROLL = RATE / 4;
const START_DELAY = 0.05;
const UNDERRUN = 0.3;
const TICK_MS = 250;

export interface OpusPackPlayerOptions {
	loop?: boolean;
	volume?: number;
	onEnded?: () => void;
	onError?: (err: unknown) => void;
}

interface Header {
	channels: number;
	preSkip: number;
	packetCount: number;
	totalSamples: number;
	loopStart: number;
	loopEnd: number;
	packetSamples: number;
}

interface Segment {
	decoder: AudioDecoder;
	start: number;
	end: number;
	next: number;
	last: number;
	flushing: boolean;
}

class OpkError extends Error {}

function parseHeader(view: DataView): Header {
	const magic = String.fromCharCode(
		view.getUint8(0),
		view.getUint8(1),
		view.getUint8(2),
		view.getUint8(3),
	);
	if (magic !== MAGIC) throw new OpkError("not an opk file");
	const version = view.getUint8(4);
	if (version !== VERSION)
		throw new OpkError(`unsupported opk version ${version}`);

	const h: Header = {
		channels: view.getUint8(5),
		preSkip: view.getUint16(6, true),
		packetCount: view.getUint32(8, true),
		totalSamples: view.getUint32(12, true),
		loopStart: view.getUint32(16, true),
		loopEnd: view.getUint32(20, true),
		packetSamples: view.getUint16(24, true),
	};
	if (h.channels < 1 || h.channels > 2)
		throw new OpkError(`unsupported channel count ${h.channels}`);
	if (!h.packetSamples || !h.packetCount)
		throw new OpkError("opk has no packets");
	if (!(h.loopStart < h.loopEnd && h.loopEnd <= h.totalSamples))
		throw new OpkError(`bad loop ${h.loopStart}..${h.loopEnd}`);
	return h;
}

function opusHead(channels: number): Uint8Array<ArrayBuffer> {
	const head = new Uint8Array(19);
	const view = new DataView(head.buffer);
	head.set(new TextEncoder().encode("OpusHead"));
	head[8] = 1;
	head[9] = channels;
	view.setUint16(10, 0, true);
	view.setUint32(12, RATE, true);

	return head;
}

export class OPKPlayer {
	private readonly uri: string;
	private readonly loop: boolean;
	private readonly onEnded?: () => void;
	private readonly onError: (err: unknown) => void;
	private readonly ctx: AudioContext;
	private readonly gain: GainNode;
	private state: "stopped" | "playing" | "paused" | "destroyed" = "stopped";
	private error: unknown = null;
	private timer = 0;

	private readonly abort = new AbortController();
	private header: Header | null = null;
	private packets: Uint8Array<ArrayBuffer>[] = [];
	private leftover = new Uint8Array(0);
	private received = 0;
	private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
	private skip = 0;
	private fetching = false;
	private eof = false;
	private retryAt = 0;
	private ranged = true;

	private gen = 0;
	private segment: Segment | null = null;
	private block: Float32Array<ArrayBuffer>[] = [];
	private blockFrames = 0;

	private base = -1;
	private framesOut = 0;
	private lastBuffer: AudioBuffer | null = null;
	private readonly sources = new Set<AudioBufferSourceNode>();
	private finished = false;

	constructor(uri: string, options: OpusPackPlayerOptions = {}) {
		this.uri = uri;
		this.loop = options.loop ?? true;
		this.onEnded = options.onEnded;
		this.onError = options.onError ?? console.error;
		this.ctx = new AudioContext({
			sampleRate: RATE,
			latencyHint: "playback",
		});
		this.ctx.suspend().catch(() => {});
		this.gain = this.ctx.createGain();
		this.gain.gain.value = options.volume ?? 1;
		this.gain.connect(this.ctx.destination);
	}

	play(): Promise<void> {
		if (this.state === "destroyed")
			return Promise.reject(new Error("player was destroyed"));

		if (this.error) return Promise.reject(this.error);

		if (typeof AudioDecoder === "undefined")
			return Promise.reject(
				new Error("WebCodecs AudioDecoder unavailable"),
			);

		const resumed = this.ctx.resume();

		if (this.state !== "playing") {
			this.state = "playing";
			this.timer = setInterval(() => this.pump(), TICK_MS);
			this.pump();
		}

		return resumed;
	}

	pause(): void {
		if (this.state !== "playing") return;

		this.state = "paused";

		clearInterval(this.timer);

		this.ctx.suspend().catch(() => {});
	}

	stop(): void {
		if (this.state === "destroyed") return;

		this.state = "stopped";

		clearInterval(this.timer);

		this.reset();
		this.ctx.suspend().catch(() => {});
	}

	setVolume(volume: number): void {
		if (this.state === "destroyed") return;

		this.gain.gain.setTargetAtTime(
			Math.max(0, volume),
			this.ctx.currentTime,
			0.015,
		);
	}

	destroy(): void {
		if (this.state === "destroyed") return;

		this.stop();
		this.state = "destroyed";
		this.abort.abort();
		this.reader?.cancel().catch(() => {});
		this.reader = null;
		this.ctx.close().catch(() => {});
		this.packets = [];
		this.leftover = new Uint8Array(0);
	}

	private reset(): void {
		this.gen++;
		if (this.segment && this.segment.decoder.state !== "closed")
			this.segment.decoder.close();

		this.segment = null;

		for (const src of this.sources) {
			src.onended = null;
			src.stop();
			src.disconnect();
		}

		this.sources.clear();
		this.blockFrames = 0;
		this.base = -1;
		this.framesOut = 0;
		this.lastBuffer = null;
		this.finished = false;
	}

	private fail(err: unknown): void {
		this.error = err;

		this.stop();
		this.onError(err);
	}

	private packetOf(sample: number): number {
		const h = this.header!;
		return Math.max(0, Math.floor((sample + h.preSkip) / h.packetSamples));
	}

	private ahead(): number {
		const end = this.base < 0 ? 0 : this.base + this.framesOut / RATE;
		return (
			Math.max(0, end - this.ctx.currentTime) + this.blockFrames / RATE
		);
	}

	private pump(): void {
		if (this.state !== "playing") return;

		this.fetchIfNeeded();

		const h = this.header;

		if (!h) return;

		if (!this.segment && !this.finished)
			this.segment = this.openSegment(
				0,
				this.loop ? h.loopEnd : h.totalSamples,
			);

		const seg = this.segment;

		if (!seg || seg.flushing) return;

		while (
			seg.next <= seg.last &&
			seg.next < this.packets.length &&
			seg.decoder.decodeQueueSize < MAX_DECODE_QUEUE &&
			this.ahead() < DECODE_AHEAD
		) {
			const i = seg.next++;
			seg.decoder.decode(
				new EncodedAudioChunk({
					type: "key",
					timestamp: Math.round((i * h.packetSamples * 1e6) / RATE),
					duration: Math.round((h.packetSamples * 1e6) / RATE),
					data: this.packets[i],
				}),
			);
		}

		if (seg.next > seg.last) {
			this.closeSegment(seg);
		} else if (seg.next >= this.packets.length) {
			if (this.eof) {
				this.fail(new OpkError(`${this.uri} is truncated`));
			} else if (this.ahead() < UNDERRUN) {
				if (this.blockFrames) this.flushBlock();
				while (this.lastBuffer && this.ahead() < UNDERRUN)
					this.schedule(this.lastBuffer);
			}
		}
	}

	private openSegment(start: number, end: number): Segment {
		const h = this.header!;
		const gen = this.gen;
		const seg: Segment = {
			decoder: new AudioDecoder({
				output: (data) => {
					if (gen === this.gen) this.take(data, seg);
					data.close();
				},
				error: (err) => {
					if (gen === this.gen) this.fail(err);
				},
			}),
			start,
			end,
			next: this.packetOf(start - PREROLL),
			last: this.packetOf(end - 1),
			flushing: false,
		};

		seg.decoder.ondequeue = () => this.pump();
		seg.decoder.configure({
			codec: "opus",
			sampleRate: RATE,
			numberOfChannels: h.channels,
			description: opusHead(h.channels),
		});

		return seg;
	}

	private closeSegment(seg: Segment): void {
		seg.flushing = true;
		const gen = this.gen;
		seg.decoder.flush().then(
			() => {
				if (gen !== this.gen) return;
				seg.decoder.close();
				const h = this.header!;
				if (this.loop) {
					this.segment = this.openSegment(h.loopStart, h.loopEnd);
				} else {
					this.segment = null;
					this.finished = true;
					if (this.blockFrames) this.flushBlock();
					if (!this.sources.size) this.end();
				}
				this.pump();
			},
			(err) => {
				if (gen === this.gen) this.fail(err);
			},
		);
	}

	private end(): void {
		this.stop();
		this.onEnded?.();
	}

	private take(data: AudioData, seg: Segment): void {
		const h = this.header!;
		const first = Math.round((data.timestamp * RATE) / 1e6) - h.preSkip;
		let from = Math.max(0, seg.start - first);
		const to = Math.min(data.numberOfFrames, seg.end - first);
		const channels = Math.min(h.channels, data.numberOfChannels);

		while (from < to) {
			const n = Math.min(to - from, BLOCK_FRAMES - this.blockFrames);
			for (let c = 0; c < h.channels; c++) {
				data.copyTo(
					this.block[c].subarray(
						this.blockFrames,
						this.blockFrames + n,
					),
					{
						planeIndex: Math.min(c, channels - 1),
						frameOffset: from,
						frameCount: n,
						format: "f32-planar",
					},
				);
			}
			this.blockFrames += n;
			from += n;
			if (this.blockFrames === BLOCK_FRAMES) this.flushBlock();
		}
	}

	private flushBlock(): void {
		const h = this.header!;
		const buffer = this.ctx.createBuffer(
			h.channels,
			this.blockFrames,
			RATE,
		);

		for (let c = 0; c < h.channels; c++)
			buffer.copyToChannel(
				this.block[c].subarray(0, this.blockFrames),
				c,
			);

		this.blockFrames = 0;
		if (buffer.length === BLOCK_FRAMES) this.lastBuffer = buffer;
		this.schedule(buffer);
	}

	private schedule(buffer: AudioBuffer): void {
		let when = this.base + this.framesOut / RATE;

		if (this.base < 0 || when < this.ctx.currentTime + 0.01) {
			this.base =
				Math.ceil((this.ctx.currentTime + START_DELAY) * RATE) / RATE;
			this.framesOut = 0;
			when = this.base;
		}

		this.framesOut += buffer.length;

		const src = this.ctx.createBufferSource();
		src.buffer = buffer;
		src.connect(this.gain);
		src.onended = () => {
			this.sources.delete(src);
			src.disconnect();
			if (this.finished && !this.sources.size) this.end();
			else this.pump();
		};
		src.start(when);
		this.sources.add(src);
	}

	private fetchIfNeeded(): void {
		if (this.fetching || this.eof || performance.now() < this.retryAt)
			return;

		const h = this.header;

		if (h) {
			const limit = this.loop
				? this.packetOf(h.loopEnd - 1) + 1
				: h.packetCount;
			const cursor = this.segment?.next ?? 0;
			const want = Math.min(
				limit,
				cursor + Math.ceil((FETCH_AHEAD * RATE) / h.packetSamples),
			);
			if (this.packets.length >= want) return;
		}

		this.fetching = true;
		this.fetchMore().then(
			() => {
				this.fetching = false;
				this.pump();
			},
			(err) => {
				this.fetching = false;
				this.reader = null;

				if (this.state === "destroyed") return;

				if (err instanceof OpkError) {
					this.fail(err);
				} else {
					console.warn(`fetching ${this.uri} failed, retrying`, err);
					this.retryAt = performance.now() + RETRY_MS;
				}
			},
		);
	}

	private async fetchMore(): Promise<void> {
		if (!this.reader) {
			const start = this.received;
			const size = this.packets.length
				? Math.max(
						MIN_FETCH_BYTES,
						Math.ceil(
							((this.received - HEADER_SIZE) /
								this.packets.length) *
								((FETCH_CHUNK * RATE) /
									this.header!.packetSamples),
						),
					)
				: FIRST_FETCH_BYTES;
			const res = await fetch(this.uri, {
				headers: this.ranged
					? { Range: `bytes=${start}-${start + size - 1}` }
					: {},
				signal: this.abort.signal,
			});
			if (res.status === 416) {
				this.endOfFile();
				return;
			}
			if (!res.ok) {
				const msg = `fetching ${this.uri} failed: HTTP ${res.status}`;
				throw res.status >= 500 ? new Error(msg) : new OpkError(msg);
			}

			if (res.status === 206) {
				const bytes = new Uint8Array(await res.arrayBuffer());
				const range = res.headers
					.get("Content-Range")
					?.match(/^bytes (\d+)-(\d+)\/(\d+|\*)$/);

				if (
					!range ||
					Number(range[1]) !== start ||
					Number(range[2]) - start + 1 !== bytes.length
				) {
					console.warn(
						`${this.uri} got a mismatched range, streaming it whole`,
					);
					this.ranged = false;
					return;
				}

				this.receive(bytes);
				if (
					bytes.length < size ||
					(range[3] !== "*" && this.received >= Number(range[3]))
				)
					this.endOfFile();
				return;
			}

			if (!res.body) throw new OpkError(`${this.uri} has no body`);
			this.reader = res.body.getReader();
			this.skip = start;
		}

		const { done, value } = await this.reader.read();
		if (done) {
			this.reader = null;
			this.endOfFile();
		} else {
			this.receive(value);
		}
	}

	private endOfFile(): void {
		this.eof = true;
		this.reader?.cancel().catch(() => {});
		this.reader = null;
	}

	private receive(chunk: Uint8Array): void {
		if (this.state === "destroyed") return;
		if (this.skip) {
			const n = Math.min(this.skip, chunk.length);
			this.skip -= n;
			chunk = chunk.subarray(n);
		}
		this.received += chunk.length;

		const buf = new Uint8Array(this.leftover.length + chunk.length);
		buf.set(this.leftover);
		buf.set(chunk, this.leftover.length);
		const view = new DataView(buf.buffer);
		let pos = 0;

		if (!this.header) {
			if (buf.length < HEADER_SIZE) {
				this.leftover = buf;
				return;
			}
			const h = parseHeader(view);
			this.header = h;
			this.block = Array.from(
				{ length: h.channels },
				() => new Float32Array(BLOCK_FRAMES),
			);
			pos = HEADER_SIZE;
		}

		const count = this.header.packetCount;
		while (this.packets.length < count && pos + 2 <= buf.length) {
			const len = view.getUint16(pos, true);
			if (pos + 2 + len > buf.length) break;
			this.packets.push(buf.subarray(pos + 2, pos + 2 + len));
			pos += 2 + len;
		}
		this.leftover = buf.slice(pos);
		if (this.packets.length === count) this.endOfFile();
	}
}

export interface OpusPackSamplesOptions {
	volume?: number;
	onError?: (err: unknown) => void;
}

export class OPKSampleRegionPlayer {
	private readonly ctx: AudioContext;
	private readonly gain: GainNode;
	private buffer: AudioBuffer | null = null;
	private destroyed = false;

	constructor(uri: string, options: OpusPackSamplesOptions = {}) {
		this.ctx = new AudioContext({
			sampleRate: RATE,
			latencyHint: "interactive",
		});
		this.gain = this.ctx.createGain();
		this.gain.gain.value = options.volume ?? 1;
		this.gain.connect(this.ctx.destination);
		this.load(uri).catch(options.onError ?? console.error);
	}

	play(start: number, end: number): void {
		const buffer = this.buffer;
		if (!buffer || this.destroyed) return;

		this.ctx.resume().catch(() => {});

		const src = this.ctx.createBufferSource();
		src.buffer = buffer;
		src.connect(this.gain);
		src.onended = () => src.disconnect();
		src.start(
			0,
			start / RATE,
			(Math.min(end, buffer.length) - start) / RATE,
		);
	}

	setVolume(volume: number): void {
		if (this.destroyed) return;

		this.gain.gain.setTargetAtTime(
			Math.max(0, volume),
			this.ctx.currentTime,
			0.015,
		);
	}

	destroy(): void {
		if (this.destroyed) return;

		this.destroyed = true;
		this.buffer = null;
		this.ctx.close().catch(() => {});
	}

	private async load(uri: string): Promise<void> {
		if (typeof AudioDecoder === "undefined")
			throw new Error("WebCodecs AudioDecoder unavailable");

		const res = await fetch(uri);
		if (!res.ok)
			throw new OpkError(`fetching ${uri} failed: HTTP ${res.status}`);
		const data = new Uint8Array(await res.arrayBuffer());
		const view = new DataView(data.buffer);
		const h = parseHeader(view);

		const planes = Array.from(
			{ length: h.channels },
			() => new Float32Array(h.totalSamples),
		);
		let failed: unknown = null;
		const decoder = new AudioDecoder({
			output: (out) => {
				const first =
					Math.round((out.timestamp * RATE) / 1e6) - h.preSkip;
				const from = Math.max(0, -first);
				const to = Math.min(out.numberOfFrames, h.totalSamples - first);
				for (let c = 0; c < h.channels && from < to; c++)
					out.copyTo(planes[c].subarray(first + from, first + to), {
						planeIndex: Math.min(c, out.numberOfChannels - 1),
						frameOffset: from,
						frameCount: to - from,
						format: "f32-planar",
					});
				out.close();
			},
			error: (err) => (failed = err),
		});
		decoder.configure({
			codec: "opus",
			sampleRate: RATE,
			numberOfChannels: h.channels,
			description: opusHead(h.channels),
		});

		let pos = HEADER_SIZE;
		for (let i = 0; i < h.packetCount; i++) {
			if (pos + 2 > data.length)
				throw new OpkError(`${uri} is truncated`);
			const len = view.getUint16(pos, true);
			if (pos + 2 + len > data.length)
				throw new OpkError(`${uri} is truncated`);
			decoder.decode(
				new EncodedAudioChunk({
					type: "key",
					timestamp: Math.round((i * h.packetSamples * 1e6) / RATE),
					duration: Math.round((h.packetSamples * 1e6) / RATE),
					data: data.subarray(pos + 2, pos + 2 + len),
				}),
			);
			pos += 2 + len;
		}
		await decoder.flush().finally(() => {
			if (decoder.state !== "closed") decoder.close();
		});
		if (failed) throw failed;
		if (this.destroyed) return;

		const buffer = this.ctx.createBuffer(h.channels, h.totalSamples, RATE);
		planes.forEach((plane, c) => buffer.copyToChannel(plane, c));
		this.buffer = buffer;
	}
}
