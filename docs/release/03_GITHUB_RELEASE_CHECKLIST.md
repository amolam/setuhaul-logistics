# Phase D: private GitHub release checklist

Nothing has been pushed. There is no GitHub remote. Every step below that touches GitHub needs the owner's approval
and the owner's own `gh` / git sign-in.

## 1. What would be pushed

- **Tracked files:** everything `git ls-files` lists: 189 files, 4.3 MiB packed; the largest are the challenge
  brief PDF (3.0 MB), the SQLite snapshot and ER diagram (~270 KB each), the readout deck (~220 KB).
  - Code: `setuhaul/` (agent, api, db, domain, notify, ops, workflow), `migrations/0001–0023`, `tests/` (23 files),
    `evals/`, `demo/`, `scripts/`
  - Docs: `docs/brief`, `docs/1-frame`, `docs/2-design`, `docs/3-build`, `docs/review`, `docs/operations`,
    `docs/release`
  - Other: `presentation/`, `data/`, root config (`README.md`, `requirements*.txt`, `pyproject.toml`, `.env.example`,
    `.gitignore`, `.gitattributes`)
- **Excluded by `.gitignore`** (present on disk, never pushed): `.claude/`, `docs/brief/21182924-SetuHaul_Data_Package.zip`
  (every `*.zip`), `__pycache__/`, any `.env` / `.env.*`, `*.key`, `*.pem`, `secrets/`, local test-cluster folders
  (`pgdata/`, `pg.log`, `pt*/`, `unz*/`).
- **Outside the repository** (not affected): `G:\FDE-Projects\setUhaul_archive\` (old review zips),
  `%USERPROFILE%\.setuhaul\openrouter.key`.

**Owner decision:** the challenge brief PDF (`docs/brief/…Project_Brief.pdf`) is third-party material. It is fine in a
**private** repository; remove it before the repository is ever made public or shared outside the review.

## 2. Pre-push checks

| Check | Status |
|---|---|
| Secret scan of tracked files and full history | ✅ clean (`02_SECURITY_SCAN.md`) |
| Dependency audit | ✅ no known vulnerabilities after the pin updates |
| Full test suite on the release commit | ✅ see `01_TEST_REPORT.md` (one run, after the final code change; later commits change documents only) |
| Dependencies portable | ✅ only `name==version` pins, no paths or local wheels; a clean virtual environment installed from them with no conflicts, audited clean, and ran 394 API/agent/provider tests |
| Booking switch | ✅ `SETUHAUL_BOOKING_ENABLED=false` in `.env.example`; default `false` in `Settings.from_env` |
| No real data | ✅ synthetic only (`02_SECURITY_SCAN.md` §1) |
| Commit author e-mail | ⚠ owner's personal address on every commit; optional rewrite to the GitHub `noreply` address before the first push (owner decision; a rewrite changes every commit id) |
| Repository visibility | must be **private** |

## 3. Commit structure (local, current)

```
main                          77e82cc  Restructure the repository into a standard layout
                              51af118  Snapshot of the local build before the repository restructure
phase-b/openrouter-provider   (main +)
                              59032f6  Phase B: OpenRouter primary provider, Secrets Manager, usage ledger and daily budget (T52)
                              47ea57e  Add the faculty readout deck, run sheet and demo-changes brief
                              6392f1f  Phase B review fixes: validate OpenRouter answers, settle unusable answers as errors, classify credit-limit 403s
                              a970616  Demo console presentation mode: AI toggle, race for one slot, reefer no-slot preset
                              e307a00  Release plan phases C-G: dependency security upgrades, /health, release documents
                              (next)   Final-review corrections to the release documents
```

Proposed after the owner's final review:

1. Fast-forward `main` to `phase-b/openrouter-provider` (no merge commit; history stays linear).
2. Create `release/fde-sydney-pilot` from `main`.
3. Tag `v0.1.0-fde-pilot` on that commit **only after** the final review accepts the test report and these checks.
4. Create the **private** GitHub repository and push `main`, `release/fde-sydney-pilot` and the tag.
5. On GitHub: enable secret scanning and push protection, protect `main` and `release/*` (no force-push, PR review).

## 4. Commands for the owner (run only after approval)

```powershell
git switch main
```

```powershell
git merge --ff-only phase-b/openrouter-provider
```

```powershell
git switch -c release/fde-sydney-pilot
```

```powershell
git tag -a v0.1.0-fde-pilot -m "SetuHaul FDE Sydney pilot, synthetic data, booking switch OFF"
```

```powershell
gh repo create setuhaul-logistics --private --source . --remote origin
```

```powershell
git push origin main release/fde-sydney-pilot v0.1.0-fde-pilot
```
