# Stateful Two-Phase Candidate-Admission API (`POST /quantize`)

Flask implementation of the freeze/select `/quantize` spec.

## Files

- `app.py` — Flask entrypoint, single `POST /quantize` route, dispatches on `phase`.
- `freeze.py` — freeze-phase validation + per-candidate status logic.
- `select_phase.py` — select-phase validation, lineage/manifest checks, scoring, admission, winner selection. (Named `select_phase.py`, not `select.py`, to avoid shadowing Python's stdlib `select` module — that collision caused every select request to 500 during local testing; renaming fixed it.)
- `common.py` — shared type checks, UTF-8 hashing, canonical-JSON package-digest routine.
- `storage.py` — SQLite-backed persistence for freeze records (survives across requests within the running container; see **Persistence caveat** below).

## Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py          # serves on :8000 (or $PORT)
```

## Run with Docker

```bash
docker build -t quantize-service .
docker run -p 8000:8000 -e PORT=8000 quantize-service
```

## Deploy on Render

1. Push this repo to GitHub.
2. Render → **New → Web Service** → connect repo → environment = **Docker**.
3. Render supplies `$PORT`; the container's `CMD` already binds gunicorn to it.
4. Paste the resulting `https://<your-service>.onrender.com` URL into the grader.

### Persistence caveat

Frozen candidates are stored in a SQLite file at `./data/freezes.db` inside
the container. This survives across requests to the *same running
instance*, which is what a normal grading session needs (freeze once,
then select against it). It does **not** survive a redeploy or an
instance restart (e.g. Render free-tier services spin down after
inactivity and lose their local disk on the next cold start). If your
grading session needs freeze/select calls to be reliably separated by
long idle periods, attach a Render **persistent disk** mounted at
`/app/data` so the SQLite file survives restarts.

The service runs a single gunicorn worker (`-w 1`) deliberately: freeze
persistence relies on a shared SQLite file plus an in-process lock for
atomic "check existing / insert new" logic. Multiple worker *processes*
would each have their own lock and could race on simultaneous first-time
freezes of the same `freezeId`. If you need more throughput, keep worker
count at 1 and raise `--threads` instead (already set to 4).

## Spec interpretation notes

The spec is precise in most places but leaves a few behaviors implicit.
Here's exactly how this implementation resolved each ambiguity — worth
checking first if grader feedback disagrees with a specific case:

**What triggers a top-level HTTP 400 `INVALID_INPUT`** (request rejected,
freezeId *not* reserved):
- Missing/unknown `phase`.
- **Freeze**: malformed `freezeId` (not a 1–128 char string), malformed
  top-level `calibrationDigest`/`tokenizerDigest`, malformed
  `allowedUnsupportedReasons` (not an array of unique non-empty strings),
  `candidates` missing/non-array/empty, or any candidate with a
  missing/blank/duplicate `name`.
- **Select**: `candidates` not an array, `rows` not an array, `policy`
  not an object, or malformed `freezeId`.

Everything else that's wrong with one specific candidate (bad `files`,
non-boolean `loadable`, blank/missing per-candidate digests, a blank
`unsupportedReason`) does **not** abort the whole freeze request — it
only marks that candidate `status:"invalid"` with `reasonCodes:
["INVALID_INPUT"]`, while sibling candidates are still processed.

**Freeze status logic**, applied in this order per candidate:
1. Structural check on `files` (non-empty object, string keys/values) →
   if it fails, `status:"invalid"`, empty inventory, null totals/digest,
   `["INVALID_INPUT"]`, regardless of other fields.
2. Structural check on `loadable`/candidate-level digests/`unsupportedReason`
   shape → same `INVALID_INPUT` outcome if any fail (inventory is still
   computed here, since `files` itself was fine).
3. If `unsupportedReason` is present: `status:"unsupported"` when it's in
   `allowedUnsupportedReasons`, else `status:"invalid"` with
   `["UNALLOWED_UNSUPPORTED_REASON"]`.
4. Otherwise: `status:"frozen"` only if `loadable===true` **and**
   candidate digests match the request's; any combination of failures
   here yields `status:"invalid"` with the corresponding subset of
   `NOT_LOADABLE` / `CALIBRATION_MISMATCH` / `TOKENIZER_MISMATCH`.

**Select-phase lineage vs. manifest** — "the supplied candidate array must
exactly equal the response stored for freezeId" is checked per candidate
name and split into two failure modes:
- Identity/status fields (`name`, `status`, `reasonCodes`) mismatching the
  stored freeze record → `INVALID_LINEAGE`.
- Manifest fields (`inventory`, `totalBytes`, `packageDigest`)
  mismatching the stored record → `INVALID_MANIFEST`.

A candidate name that isn't in the stored freeze response at all, or
whose stored status wasn't `"frozen"`, is `NOT_FROZEN` (checked first;
lineage/manifest checks only apply to candidates that actually froze).

`totalBytes`/`packageDigest` used for **all** policy comparisons and the
result object always come from the *stored* freeze record — the wire
value in the submitted candidate is only used for the lineage/manifest
comparison above, never trusted for size-limit math.

**Required slices**: accuracy is computed only for slice names present in
`policy.requiredSlices`. A required slice with zero matching rows is
`MISSING_SLICE:<name>` and is simply omitted from the returned `slices`
object (there's no ratio to report).

**Invalid policy** (bad field types, or `candidateOrder`'s name set not
matching the submitted candidates' name set) adds `INVALID_POLICY` to
every candidate and blocks admission for all of them, but doesn't
suppress `aggregate`/`slices` computation (those only depend on
`rows`/predictions, not on the policy).

**Winner selection**: among `admitted:true` results, pick smallest
`totalBytes`, tie-break by lowest `latencyMs`, tie-break by
`candidateOrder` position. `packageManifest` is the *stored freeze
record object* for that winner (not the select-phase result object).

## Testing performed

Exercised locally via Flask's test client (not through a live grader,
since this sandbox has no network egress to actually hit a deployed
instance): freeze success, replay (identical body → same response, 200),
conflict (different body, same `freezeId` → 409), empty-candidates 400,
unknown-phase 400, unsupported/disallowed-reason/not-loadable/
digest-mismatch/bad-files/missing-field candidates, end-to-end select
with mixed admit/reject, tampered manifest → `INVALID_MANIFEST`, size and
latency limit rejection, missing required slice, all four select 400
shapes, and an unknown `freezeId` → `NOT_FROZEN`. All matched expected
behavior under this implementation's interpretation of the spec.
