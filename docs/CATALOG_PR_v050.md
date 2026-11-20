# Catalog PR prep — ceo-pack v0.5.0 (do NOT file yet)

Adrian files this PR himself (or says go) after the v0.5.0-dev branch is pushed
and reviewed. One file changes in NousResearch/hermes-agent:
`plugin-catalog/ceo-pack.yaml`. Same route as PR #155549 (v0.4.0) and #157203
(v0.4.1): pencil-edit the file in the GitHub web UI (auto-forks), branch
`ceo-pack-0.5.0`, validate first with `scripts/validate_plugin_catalog.py`.

## The image line (the install-card thumbnail)

Layout copied from the advanced-delegation catalog entry
(`plugin-catalog/advanced-delegation.yaml` in the hermes-agent checkout):
the catalog `image:` is a `raw.githubusercontent.com` URL pointing at an
`assets/` file in the plugin repo, pinned to the entry's SHA — so the
thumbnail is as immutable as the sha pin. The validator
(`scripts/validate_plugin_catalog.py`, `_is_allowed_image_url`) only accepts
https URLs on `raw.githubusercontent.com` / `github.com` /
`*.githubusercontent.com`.

```
image: https://raw.githubusercontent.com/tradersurfer/ceo-pack/9df6e4f13d37b830da74f2e9ed825bdc1e3f5620/assets/ceo-pack-cover-v040.jpg
```

`9df6e4f1...` is the HEAD of `v0.5.0-dev` after the command-fix bundle push (2026-11-20 ~00:48). Re-pin if the branch moves again before filing — the /ceo short-reply fix is built and verified but NOT pushed yet, so the SHA WILL move once more. Do NOT substitute the v0.4.1 sha (6331b08a) — the image must resolve at the pinned v0.5.0 commit.

VERIFIED 2026-11-20 ~13:10: the `.jpg` URL above returns HTTP 200, `image/jpeg`, 447,488 bytes at `9df6e4f`. The `.webp` URL 404s at this SHA — Adrian renamed the cover to `.jpg` in commit `581f0b02` (2026-11-19 ~23:33 EST, "Rename cover to .jpg (JPEG bytes)"), so `.webp` no longer exists on the branch. The draft PR (`~/workspace/your_files/ceo-pack-v050-nous-pr-draft.md`) uses the `.jpg` URL. The local README already links the `.jpg` name — that line is now pushable, since the `.jpg` exists on the branch.

## Entry changes (vs the v0.4.1 entry, PR #157203)

- `sha`: 6331b08a03f18c202a292eb8c4741b69aad69994 → `<V050_SHA>`
- `version`: `"0.4.1"` → `"0.5.0"`
- `description`: ELI5, matching the rewritten `plugin.yaml` description —
  "Your AI C-suite inside Hermes. Say what your business needs in plain
  words — the pack plans it, assigns each job to the right seat, checks the
  work, and stops for your approval before anything big, priced, or hard to
  undo." (Replaces the v0.4.1 wording; the plugin.yaml description carries
  the longer version with the short dev note.)
- `image`: add the line above (new — v0.4.0/v0.4.1 entries had no image,
  which is why the install card has no thumbnail).
- `requires_hermes`: `">=0.22.5"` (already the plugin.yaml value for v0.5.0;
  confirm against the entry — v0.4.1 said `>=0.21.6`).

## The rename — DONE (Adrian, 2026-11-19 ~23:33 EST)

Adrian ran the one-liner: commit `581f0b02` "Rename cover to .jpg (JPEG bytes)" is on `v0.5.0-dev`. The branch now carries `assets/ceo-pack-cover-v040.jpg` (JPEG bytes, 1920x1280, 447,488 bytes) and NO `.webp` file — the `.webp` URL 404s at the current HEAD. The image: line uses `.jpg` (verified 200 at `9df6e4f`, Nov 20 ~13:10). The local README already links the `.jpg` name, so that line is now pushable.

## Pre-flight (before filing)

1. Push `v0.5.0-dev` to GitHub; record the HEAD SHA.
2. Confirm the image URL loads in a browser at that SHA.
3. Run `scripts/validate_plugin_catalog.py` on the edited entry (rc=0).
4. File the PR; note the fork-PR checks wait for a Nous maintainer to
   approve the run (normal hold, not a rejection).
