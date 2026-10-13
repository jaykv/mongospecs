// Mongoose comparator (plan node E2).
//
// Runs the equivalents of workloads B1, B2, B4 and B8 with Mongoose against the
// same MongoDB and the same document shapes, and reports Mongoose's overhead
// over the raw Node driver. Absolute times are not comparable across runtimes;
// the overhead ratios are what is set next to mongospecs' ratios over pymongo.
//
//   cd bench/mongoose && npm install && npm run bench [-- --quick]

import { execSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import mongoose from 'mongoose';

const QUICK = process.argv.includes('--quick');
const URI = process.env.MONGOSPECS_TEST_URI || 'mongodb://localhost:27117/?replicaSet=rs0';
const REPEAT = QUICK ? 3 : 15;
const here = dirname(fileURLToPath(import.meta.url));

function rawDocument(shape, n) {
  const when = new Date(Date.UTC(2024, 0, 1) + n * 60000);
  if (shape === 'small') {
    return { n, name: `name-${n}`, score: n * 1.5, active: n % 2 === 0, created: when };
  }
  return {
    n,
    customer: `customer-${n}`,
    address: { street: `${n} Main St`, city: 'Springfield', zip: String(n % 100000).padStart(5, '0') },
    lines: Array.from({ length: 8 }, (_, i) => ({ sku: `sku-${n}-${i}`, qty: i + 1, price: 9.99 + i })),
    tags: Array.from({ length: 5 }, (_, i) => `tag-${(n + i) % 7}`),
    meta: { source: 'bench', batch: String(Math.floor(n / 100)) },
    placed: when,
  };
}

const Small = mongoose.model(
  'Small',
  new mongoose.Schema(
    { n: Number, name: String, score: Number, active: Boolean, created: Date },
    { collection: 'bench_small', versionKey: false },
  ),
);
const Nested = mongoose.model(
  'Nested',
  new mongoose.Schema(
    {
      n: Number,
      customer: String,
      address: new mongoose.Schema({ street: String, city: String, zip: String }, { _id: false }),
      lines: [new mongoose.Schema({ sku: String, qty: Number, price: Number }, { _id: false })],
      tags: [String],
      meta: { type: Map, of: String },
      placed: Date,
    },
    { collection: 'bench_nested', versionKey: false },
  ),
);
const Plain = mongoose.model(
  'Plain',
  new mongoose.Schema({ n: Number, name: String }, { collection: 'bench_plain', versionKey: false }),
);
const Managed = mongoose.model(
  'Managed',
  new mongoose.Schema(
    { n: Number, name: String },
    { collection: 'bench_managed', timestamps: true, optimisticConcurrency: true },
  ),
);
const MODELS = { small: Small, nested: Nested };

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function measure(fn, { setup } = {}) {
  for (let i = 0; i < 2; i++) {
    if (setup) await setup();
    await fn();
  }
  const samples = [];
  for (let i = 0; i < REPEAT; i++) {
    if (setup) await setup();
    const start = process.hrtime.bigint();
    await fn();
    samples.push(Number(process.hrtime.bigint() - start) / 1e6);
  }
  return median(samples);
}

const round = (value, digits = 3) => Number(value.toFixed(digits));
const ratio = (value, base) => (base ? round(value / base, 2) : 0);
const rows = [];

async function materialise() {
  const n = QUICK ? 300 : 10000;
  for (const [shape, Model] of Object.entries(MODELS)) {
    const docs = Array.from({ length: n }, (_, i) => ({ _id: new mongoose.Types.ObjectId(), ...rawDocument(shape, i) }));
    const encoded = docs.map((d) => mongoose.mongo.BSON.serialize(d));
    const base = await measure(() => encoded.map((b) => mongoose.mongo.BSON.deserialize(b)));
    rows.push({ workload: 'B1', shape, case: 'driver bson->object', n, us_per_doc: round((base * 1000) / n, 2), vs_driver: 1 });
    const hydrated = await measure(() => docs.map((d) => Model.hydrate(d)));
    rows.push({ workload: 'B1', shape, case: 'Model.hydrate', n, us_per_doc: round((hydrated * 1000) / n, 2), vs_driver: ratio(hydrated, base) });
  }
}

async function finds() {
  const total = QUICK ? 300 : 10000;
  for (const [shape, Model] of Object.entries(MODELS)) {
    await Model.collection.deleteMany({});
    await Model.collection.insertMany(Array.from({ length: total }, (_, i) => rawDocument(shape, i)));
    for (const n of [1, 100, 10000].filter((size) => size <= total)) {
      const raw = await measure(() => Model.collection.find({}, { limit: n }).toArray());
      const lean = await measure(() => Model.find().limit(n).lean());
      const full = await measure(() => Model.find().limit(n));
      rows.push({ workload: 'B2', shape, case: 'driver objects', n, median_ms: round(raw), vs_driver: 1 });
      rows.push({ workload: 'B2', shape, case: 'mongoose lean()', n, median_ms: round(lean), vs_driver: ratio(lean, raw) });
      rows.push({ workload: 'B2', shape, case: 'mongoose documents', n, median_ms: round(full), vs_driver: ratio(full, raw) });
    }
  }
}

async function writes() {
  const n = QUICK ? 50 : 1000;
  const ops = QUICK ? 20 : 200;
  const reset = async () => {
    await Plain.collection.deleteMany({});
    await Managed.collection.deleteMany({});
  };
  const make = () => Array.from({ length: n }, (_, i) => ({ n: i, name: `n${i}` }));
  const rawInsert = await measure(() => Plain.collection.insertMany(make()), { setup: reset });
  const plainInsert = await measure(() => Plain.insertMany(make()), { setup: reset });
  const managedInsert = await measure(() => Managed.insertMany(make()), { setup: reset });
  rows.push({ workload: 'B4', op: `insert ${n}`, case: 'driver insertMany', median_ms: round(rawInsert), vs_driver: 1 });
  rows.push({ workload: 'B4', op: `insert ${n}`, case: 'Model.insertMany (no plugins)', median_ms: round(plainInsert), vs_driver: ratio(plainInsert, rawInsert) });
  rows.push({ workload: 'B4', op: `insert ${n}`, case: 'Model.insertMany (timestamps+versioning)', median_ms: round(managedInsert), vs_driver: ratio(managedInsert, rawInsert) });

  await reset();
  const docs = await Plain.insertMany(Array.from({ length: ops }, (_, i) => ({ n: i, name: `n${i}` })));
  const ids = docs.map((d) => d._id);
  const rawPatch = await measure(async () => {
    for (const id of ids) await Plain.collection.updateOne({ _id: id }, { $inc: { n: 1 } });
  });
  const odmPatch = await measure(async () => {
    for (const id of ids) await Plain.findByIdAndUpdate(id, { $inc: { n: 1 } }, { new: true });
  });
  const odmSave = await measure(async () => {
    for (const doc of docs) {
      doc.n += 1;
      await doc.save();
    }
  });
  rows.push({ workload: 'B4', op: `patch x${ops}`, case: 'driver updateOne', median_ms: round(rawPatch), vs_driver: 1 });
  rows.push({ workload: 'B4', op: `patch x${ops}`, case: 'findByIdAndUpdate (returns doc)', median_ms: round(odmPatch), vs_driver: ratio(odmPatch, rawPatch) });
  rows.push({ workload: 'B4', op: `patch x${ops}`, case: 'doc.save() delta', median_ms: round(odmSave), vs_driver: ratio(odmSave, rawPatch) });
  await reset();
}

async function tracking() {
  const n = QUICK ? 200 : 5000;
  await Nested.collection.deleteMany({});
  await Nested.collection.insertMany(Array.from({ length: n }, (_, i) => rawDocument('nested', i)));
  const lean = await measure(() => Nested.find().lean());
  const full = await measure(() => Nested.find());
  const heap = async (query) => {
    globalThis.gc?.();
    const before = process.memoryUsage().heapUsed;
    const held = await query();
    globalThis.gc?.();
    const used = (process.memoryUsage().heapUsed - before) / held.length;
    return Math.round(used);
  };
  const leanBytes = await heap(() => Nested.find().lean());
  const fullBytes = await heap(() => Nested.find());
  rows.push({ workload: 'B8', mode: 'lean()', n, median_ms: round(lean, 2), vs_lean: 1, bytes_per_doc: leanBytes });
  rows.push({ workload: 'B8', mode: 'documents (tracked)', n, median_ms: round(full, 2), vs_lean: ratio(full, lean), bytes_per_doc: fullBytes });
  await Nested.collection.deleteMany({});
}

function git(args) {
  try {
    return execSync(`git ${args}`, { cwd: here, encoding: 'utf8' }).trim();
  } catch {
    return 'unknown';
  }
}

const database = `ms_bench_mongoose_${Date.now().toString(36)}`;
await mongoose.connect(URI, { dbName: database });
try {
  await materialise();
  await finds();
  await writes();
  await tracking();
  const info = await mongoose.connection.db.admin().serverInfo();
  const meta = {
    git_sha: git('rev-parse --short HEAD'),
    timestamp: new Date().toISOString(),
    topology: 'rs',
    quick: QUICK,
    mongodb: info.version,
    node: process.version,
    versions: { mongoose: mongoose.version, driver: mongoose.mongo.BSON ? 'bundled with mongoose' : 'unknown' },
    platform: `${process.platform}-${process.arch}`,
  };
  const folder = join(here, '..', 'results', meta.git_sha);
  mkdirSync(folder, { recursive: true });
  const path = join(folder, `mongoose${QUICK ? '-quick' : ''}.json`);
  const payload = {
    meta,
    workload: 'E2',
    title: 'Mongoose comparator',
    question: "What is Mongoose's overhead over the raw Node driver for the same workloads?",
    rows,
    notes: [
      'Ratios are against the raw Node driver in the same process. Compare them with mongospecs ratios against pymongo, not absolute times.',
    ],
    conclusion: '',
  };
  writeFileSync(path, JSON.stringify(payload, null, 2) + '\n');
  console.table(rows);
  console.log(`-> ${path}`);
} finally {
  await mongoose.connection.dropDatabase();
  await mongoose.disconnect();
}
