# Moving to a HyperCore-managed source: requirements

**Status: documentation only. No fork exists, nothing has been created or pushed.** This is the list of what has to be true
before HyperCore builds from a repository it manages, so that the move can be reviewed and approved first. The registry already
has the fields for it (`role`, `upstream`, `sync`, `credits`, see `SOURCES.md`).

## Decisions needed before anything is created

| Decision | Options | Why it matters |
|---|---|---|
| Where the fork lives | Your user account, or a GitHub organisation | Who owns it, and who can be given access later |
| Name | For example `HyperCore-Duchamp-6.1` | It appears in every report and link |
| Visibility | Public, or private with a token | The build clones it anonymously today. A private repository needs a token in every job |
| Who may push to managed branches | You only, or named people | Managed branches decide what is built and flashed |
| Who runs the sync, and how often | By hand, or a scheduled workflow with its own token | A scheduled sync needs write access stored as a secret |
| Licence statement | Kernel code stays GPL-2.0-only | See the attribution rules below |

None of these is decided.

## Creating the fork

1. Create it as a **GitHub fork** of the upstream project, or push a full mirror with all history and tags. Never import a
   snapshot, squash, or "start from a zip": that loses authorship and breaks version resolution.
2. Keep read-only mirror branches of the upstream branches, named `upstream/<branch>` (for example `upstream/GKID-6.1`). They only
   ever move forward, by fast-forward, to match upstream. Nobody commits on them.
3. HyperCore's own branches are separate, for example `hypercore/6.1` and `hypercore/6.1-nh`. They start at the matching upstream
   tip. Registry `branch` and `branch_variants` point to these, never to `upstream/*`.
4. **Branch protection** on `hypercore/*` and `upstream/*`: no force-push, no deletion, changes by pull request with the CI checks
   passing. In the repository settings, **disable squash merging and rebase merging**. Only merge commits are allowed.
5. HyperCore changes inside the fork stay few and small. Patches the pipeline applies while building (KernelSU variants, SUSFS, the
   NTSync patch and others) are **not** committed to the fork, so the fork does not drift from upstream.
6. Commits made by HyperCore carry a clear prefix (`hypercore:`) and a `Signed-off-by` line.

## Attribution and licence

- The original `COPYING` file, licence headers and SPDX lines stay untouched. Kernel code stays GPL-2.0-only. The build scripts of
  this repository keep their own licence.
- Every upstream commit keeps its author, committer, date and message, and its **original commit SHA**. That is what merge-only
  syncing guarantees and why rebasing, squashing and cherry-picking as a way of syncing are not allowed.
- The fork's README says what it is, names the upstream project and link, and states that the code originated there.
- The registry entry lists the original authors under `credits`, with link and licence, and records the upstream repository,
  branch and the last upstream commit merged (`upstream.synced_commit`).
- No report, notification, release note or file may present upstream code as written by HyperCore. Reports always label a managed
  source and its upstream separately (`tests/test_report_provenance.py`).

## Syncing from upstream: merge only

Procedure for one sync. A person approves it, whether or not a workflow prepares it.

1. **Check upstream did not rewrite history.** The commit recorded in `upstream.synced_commit` must still be an ancestor of the
   upstream branch tip (`git merge-base --is-ancestor <synced_commit> upstream/<branch>`). If it is not, upstream force-pushed or
   rewrote history. **Stop.** Tag the old mirror tip `upstream-archive/<date>`, and decide by hand. Nothing is merged automatically.
2. Fast-forward `upstream/<branch>` to the new upstream tip.
3. Create `sync/<date>` from `hypercore/<line>` and run `git merge --no-ff upstream/<branch>`. A merge commit is always created.
4. **Conflicts.**
   - A person resolves every conflict, in the pull request, never automatically.
   - Forbidden: `-X ours`, `-X theirs`, `--allow-unrelated-histories`, `git checkout --ours/--theirs` applied to whole files, and any
     rebase or squash to "make it apply". They drop code without a decision.
   - Each conflicted file and the decision are written in the merge commit message. By default upstream's side wins, and a
     HyperCore change is kept only when the message says why.
   - A conflict inside a file HyperCore changed is a signal the HyperCore change may need to shrink or go. It is discussed in the pull
     request.
5. **Checks before merging the pull request:**
   - every upstream commit is still reachable from the branch (the upstream tip is an ancestor of `sync/<date>`);
   - no upstream commit was rewritten, so authors and SHAs match upstream's;
   - the Linux version in the Makefile never goes down along the first-parent history, because version selection relies on that;
   - `COPYING`, licence headers and the credits are present;
   - `bash tests/run_all.sh` passes, and a build of the sync branch succeeds. Build it with `KERNEL_REPO_6_1` and
     `KERNEL_BRANCH_6_1` set to the sync branch. The reports label that run `custom`.
6. Merge the pull request with a **merge commit**.
7. In a HyperCore pull request, update `upstream.synced_commit` in `sources.json`, and record the build in `versions.verified` with
   its evidence. Boot evidence is recorded only when someone booted it on a device.

## When upstream updates

- Checking for new upstream commits is cheap and read-only, so it can run on a schedule. It only reports (an issue or a summary).
  It does not merge.
- A new upstream Linux version appears in the history of the managed source only after a sync. `latest-lts` therefore reports
  "the source does not have it yet" until then, and never builds an older release in its place.
- If upstream deletes or renames the branch, the managed branches keep working. The registry's `upstream` entry is updated by a
  person.

## What the tooling will check (not built yet)

A script, run by CI on every pull request to a managed branch and before every sync, enforcing the points above:

- the upstream tip is an ancestor of the managed branch, and every upstream commit is reachable with unchanged author and SHA;
- the first-parent Linux version never decreases;
- the licence files and `credits` are present, and `role`, `upstream` and `sync` in the registry match the repository;
- HyperCore-only commits are listed, so the size of the divergence is visible.

## Leaving

Setting the source back to `"role": "external"` with the upstream repository and branch makes HyperCore build directly from the
upstream project again. The registry change is the only one needed. Reports then label the source as the upstream project.

## Risks and open questions

- A scheduled sync needs a token with write access. Where it is stored and who can use it is a security decision.
- The Duchamp upstream does not promise to keep its history. Step 1 of the sync is the protection, and it can stop a sync.
- Whether the fork is public or private changes how every build job clones it.
- Merge-only syncing keeps authorship but produces many merge commits over time. That is accepted: it is the price of keeping history.
