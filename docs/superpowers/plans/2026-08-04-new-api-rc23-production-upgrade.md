# New API rc.23 Production Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Upgrade the only production New API on FNOS from official rc.21 to official rc.23, preserve and verify its SQLite state, then expose only multimodal capabilities that pass real end-to-end requests through New API.

**Architecture:** Pin the rc.23 multi-architecture manifest digest in the only production Compose file, add a bounded production-only multimodal probe by reusing the repository's removed but tested Aurora capability-probe logic, and deploy through a stopped-New-API cold-backup gate. Rebuild only new-api; verify the console and core chat before any multimodal request; restore rc.21 plus the complete cold backup if the upgrade gate fails.

**Tech Stack:** Docker Compose, FNOS Docker, SQLite, Python 3 standard library, ffprobe 7.1.3, unittest, external-browser extension, Git bundle.

## Global Constraints

- Target image: calciumion/new-api@sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695.
- Expected Linux AMD64 child digest: sha256:3811fa4be0f4ba2ab06651de3b6818cb52c4afa7eb04a467d63492cbb5f0830c.
- Never use latest, install a dependency, or add a second long-lived New API instance.
- Do not modify or recreate Aurora, Mihomo, MetaCubeXD, n8n, SMTP, PostgreSQL, cron, or any other workflow.
- Do not read or emit passwords, client tokens, channel keys, cookies, ChatGPT tokens, media payloads, response text, original logs, or command output containing secrets.
- Browser interaction is limited to the existing New API console and existing browser login state; if authentication is required, stop and ask the user to sign in.
- A console channel-test success is supporting evidence only; a real OpenAI-compatible response with valid structure or decodable media is required for PASS.
- If the rc.23 upgrade gate fails, restore the rc.21 digest and the complete cold backup before any multimodal work.
- If only a multimodal check fails, investigate once, perform at most one targeted real retest after a concrete correction, and hide the still-failing model/Token/ability without rolling back rc.23.
- Keep the rc.21 image and the verified upgrade backup until the user separately authorizes cleanup.

---

### Task 1: Pin the rc.23 production image with a regression test

**Files:**
- Modify: tests/test_production_compose_contract.py
- Modify: docker-compose.yml

**Interfaces:**
- Consumes: The existing single docker-compose.yml and ProductionComposeContractTests.
- Produces: test_new_api_uses_the_approved_rc23_digest() and a production New API service pinned to the exact rc.23 manifest.

- [ ] **Step 1: Add the failing digest and topology test**

Add this method to ProductionComposeContractTests:

    def test_new_api_uses_the_approved_rc23_digest(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("# 2026-08-04 目标基线：v1.0.0-rc.23", text)
        self.assertIn(
            "calciumion/new-api@sha256:"
            "bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695",
            text,
        )
        self.assertEqual(text.count("container_name: new-api"), 1)
        self.assertIn("- ./data/new-api:/data", text)
        self.assertNotIn("calciumion/new-api:latest", text)

- [ ] **Step 2: Run the focused test and verify RED**

Run:

    python -m unittest tests.test_production_compose_contract.ProductionComposeContractTests.test_new_api_uses_the_approved_rc23_digest -v

Expected: FAIL because docker-compose.yml still contains the rc.21 digest and comment.

- [ ] **Step 3: Replace only the New API comment and digest**

Change only the New API image block:

    new-api:
      # 2026-08-04 目标基线：v1.0.0-rc.23
      image: calciumion/new-api@sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695

Do not alter its port, volume, environment, restart policy, container name, or neighboring services.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again.

Expected: PASS.

---

### Task 2: Add a production-only New API multimodal probe

**Files:**
- Create: scripts/new_api_multimodal_probe.py
- Create: tests/test_new_api_multimodal_probe.py
- Create: docs/contracts/new-api-multimodal-report-v1.schema.json
- Reference only: Git object 3ca3727^:scripts/aurora_capability_canary.py
- Reference only: Git object 3ca3727^:tests/test_aurora_capability_canary.py

**Interfaces:**
- Consumes: Fixed root /vol1/1000/Solis_Aurora_Gateway, SQLite data/new-api/one-api.db, fixed URL http://127.0.0.1:3000, an eligible client token selected in memory, and FNOS ffprobe.
- Produces: main(argv: list[str] | None) -> int, one bounded JSON report, and exact checks models, chat_nonstream, chat_stream, responses_nonstream, responses_stream, files, vision, image_generation, image_edit, image_variation, audio_speech, audio_transcription, audio_translation, audio_translation_composed.

- [ ] **Step 1: Write safety-gate tests before the implementation**

Create tests/test_new_api_multimodal_probe.py with tests equivalent to:

    class SafetyGateTests(unittest.TestCase):
        def test_real_api_requires_explicit_flag(self):
            with mock.patch.object(MODULE, "run_matrix") as run:
                self.assertEqual(MODULE.main([]), 2)
            run.assert_not_called()

        def test_target_is_fixed_to_production_new_api(self):
            self.assertEqual(MODULE.NEW_API_BASE_URL, "http://127.0.0.1:3000")
            self.assertFalse(hasattr(MODULE.parse_args([]), "base_url"))

        def test_client_token_is_selected_without_being_reported(self):
            token = MODULE.read_client_token(self.database, now=1_786_000_000)
            self.assertEqual(token, "sk-test-client")
            report = MODULE.serialize_report(passing_report())
            self.assertNotIn("sk-test-client", report)

The SQLite fixture must use the eligibility predicate already used by scripts/check_stack_health.py: status equals 1, not expired, and unlimited quota or positive remaining quota.

- [ ] **Step 2: Run the new test module and verify RED**

Run:

    python -m unittest tests.test_new_api_multimodal_probe -v

Expected: FAIL because the module and interfaces do not exist.

- [ ] **Step 3: Reuse and narrow the historical probe implementation**

Build scripts/new_api_multimodal_probe.py from the historical probe's tested bounded HTTP reads, SSE parsing, multipart encoding, PNG validation, MP3/ffprobe validation, result validation, and atomic report writing. Remove all canary and direct-target concepts.

Implement these exact constants and interfaces:

    NEW_API_BASE_URL = "http://127.0.0.1:3000"
    EXPECTED_ROOT = Path("/vol1/1000/Solis_Aurora_Gateway")
    CHAT_MODEL = "gpt-4o"
    IMAGE_MODEL = "gpt-image-2"
    TTS_MODEL = "tts-1"
    TRANSCRIPTION_MODEL = "whisper-1"

    EXPECTED_CHECKS = (
        "models", "chat_nonstream", "chat_stream",
        "responses_nonstream", "responses_stream", "files", "vision",
        "image_generation", "image_edit", "image_variation",
        "audio_speech", "audio_transcription", "audio_translation",
        "audio_translation_composed",
    )

    def read_client_token(database_path: Path, now: int) -> str:
        database = sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=30
        )
        try:
            row = database.execute(
                """
                SELECT key FROM tokens
                WHERE status = 1
                  AND (expired_time = -1 OR expired_time > ?)
                  AND (unlimited_quota = 1 OR remain_quota > 0)
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
        finally:
            database.close()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise ProbeError("credential_unavailable")
        return row[0] if row[0].startswith("sk-") else f"sk-{row[0]}"

    def parse_args(argv=None):
        parser = argparse.ArgumentParser()
        parser.add_argument("--allow-real-api", action="store_true")
        parser.add_argument("--root", type=Path, default=EXPECTED_ROOT)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--json", action="store_true")
        return parser.parse_args(argv)

Create the English WAV fixture in memory. Native translation passes only with stable English markers. The composed check transcribes the same fixture and sends only that in-memory text to gpt-4o; it passes only when the result contains Chinese characters and does not echo the complete English fixture sentence.

- [ ] **Step 4: Lock the sanitized report contract**

Create docs/contracts/new-api-multimodal-report-v1.schema.json with top-level required fields schema_version, checked_at, overall, checks; schema_version must equal 1; overall is PASS or FAIL; checks must contain exactly 14 items with only name, status, code and details.

In Python, allow only booleans, bounded counts, media type, byte count, codec, sample rate, channels, duration bucket and sanitized error codes. Reject response text, URLs, base64, identifiers, headers, exceptions, logs and credentials.

- [ ] **Step 5: Add transport and structure regression tests**

Port the historical tests for one request per check and zero retries; allowlisted paths; HTTP classification; non-stream and SSE structure; PNG decoding; multipart image fields; MP3 validation through mocked ffprobe stdin; transcription and translation markers; native translation failure when output remains Chinese; composed translation PASS; atomic output; maximum 32 KiB report.

Assert that serialized reports contain none of the fixture token, prompts, transcription, translation, image bytes, audio bytes, URL or exception text.

- [ ] **Step 6: Run the focused probe tests and verify GREEN**

Run:

    python -m unittest tests.test_new_api_multimodal_probe -v

Expected: all tests PASS with injected transports and zero real requests.

---

### Task 3: Update formal documentation without claiming deployment

**Files:**
- Modify: README.md
- Modify: DESIGN.md
- Modify: docs/fnos_deployment.md
- Modify: docs/workbuddy_custom_models.md

**Interfaces:**
- Consumes: The rc.23 target and probe contract.
- Produces: Target-state instructions that remain distinct from FNOS-verified facts.

- [ ] **Step 1: Update target version and immutable image facts**

Replace target rc.21 references with rc.23 and the approved manifest. Preserve dated historical rc.21 evidence.

Add this warning:

    rc.21 → rc.23 包含认证 Session、AuthFlow、渠道、Token、模型和 relay 层迁移。
    升级必须停止 New API 后冷备份整个 data/new-api，并在失败时同时恢复旧 digest
    和完整数据；不能只把镜像标签改回去。

- [ ] **Step 2: Document the multimodal acceptance rule**

Record that rc.23 remains a target until FNOS acceptance passes. State that gpt-image-2, gpt-4o, tts-1 and whisper-1 remain hidden unless real New API endpoint checks pass. State that native audio translation remains hidden unless it returns English.

- [ ] **Step 3: Document the production probe interface**

Add:

    python3 scripts/new_api_multimodal_probe.py \
      --root /vol1/1000/Solis_Aurora_Gateway \
      --allow-real-api \
      --output /tmp/new-api-multimodal-report.json \
      --json

Clarify that the script reads one eligible client token from SQLite only in memory and never stores the token or media payload.

---

### Task 4: Run local verification and prepare the implementation commit

**Files:**
- Verify all Task 1-3 files.

**Interfaces:**
- Consumes: Version pin, probe, tests, contract and docs.
- Produces: A verified local change set; commit, push and FNOS delivery remain separately gated.

- [ ] **Step 1: Run focused and full tests**

Run:

    python -m unittest tests.test_production_compose_contract tests.test_new_api_multimodal_probe -v
    python -m unittest discover -s tests -p "test_*.py" -v

Expected: all applicable tests PASS; only the documented Windows symlink test may skip.

- [ ] **Step 2: Run WSL Compose validation**

Run:

    wsl -e sh -lc 'cd /mnt/f/70_Infrastructure_and_Operations/Solis_Aurora_Gateway && SESSION_SECRET=test-session AURORA_AUTHORIZATION=test-authorization NAS_LAN_IP=192.0.2.10 docker compose config --quiet'

Expected: exit 0 and no rendered secret output.

- [ ] **Step 3: Run repository hygiene checks**

Run:

    git diff --check
    git check-ignore -v .env .secrets/session_tokens.txt data/new-api/one-api.db backups/probe.db TODO.md DEVLOG.md
    git status --short

Expected: only intended tracked files are changed and every protected path is ignored.

- [ ] **Step 4: Stop for explicit commit and push authorization**

Report the exact file list, tests, target digest and remaining live gates. Do not stage, commit, push, bundle or connect the browser without explicit approval.

After approval, stage only approved files, run git diff --cached --check and commit with:

    git commit -m "升级 New API rc.23 并准备多模态验收"

---

### Task 5: Deliver the approved implementation commit to FNOS

**Files:**
- Runtime target: /vol1/1000/Solis_Aurora_Gateway
- Temporary bundle paths: exact paths approved at the delivery gate.

**Interfaces:**
- Consumes: Approved implementation commit and clean FNOS main at the approved base.
- Produces: Clean FNOS checkout at the implementation commit without GitHub pull.

- [ ] **Step 1: Request push and bundle authorization**

Request exact permission to push the implementation commit and fast-forward FNOS using a bundle containing only the approved base-to-target range.

- [ ] **Step 2: Verify Git gates**

Require exact cwd, main, expected HEAD, clean worktree, one worktree, approved origin, live GitHub target equality, diff check, show-ref and fsck.

- [ ] **Step 3: Push and apply the exact bundle**

Push main, create only the approved commit range, verify it on Windows and FNOS, fetch from the bundle and merge with --ff-only.

- [ ] **Step 4: Delete both bundle files**

Require FNOS HEAD equality, clean status, expected path range, one worktree and fsck before deleting the exact temporary files.

---

### Task 6: Pull and verify rc.23 without starting it

**Files:**
- FNOS Docker image store only.

**Interfaces:**
- Consumes: Official rc.23 digest.
- Produces: Verified local image while production remains rc.21.

- [ ] **Step 1: Request exact image-pull authorization**

Request permission to pull only:

    calciumion/new-api@sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695

- [ ] **Step 2: Pull the digest**

Run:

    docker pull calciumion/new-api@sha256:bacbbfbed64b4579213316e0ed78415985223bb20c47fbc24572dd7be5aa1695

Do not run Compose in this task.

- [ ] **Step 3: Verify provenance**

Report only booleans for RepoDigest, linux/amd64, expected child digest, official GitHub source, version rc.23, AGPL-3.0 and non-empty revision. Keep the image after success.

---

### Task 7: Cold-back up and directly upgrade production

**Files:**
- Source: /vol1/1000/Solis_Aurora_Gateway/data/new-api
- Backup: /vol1/1000/Solis_Aurora_Gateway/backups/.new-api-rc23-upgrade-20260804

**Interfaces:**
- Consumes: Clean FNOS checkout and verified rc.23 image.
- Produces: rc.23 production New API or a restored rc.21 service.

- [ ] **Step 1: Request maintenance-window authorization**

Request permission to stop and recreate only new-api, create the exact backup, allow rc.23 SQLite migration, and restore rc.21 plus the full backup if a core gate fails.

- [ ] **Step 2: Capture pre-upgrade metadata**

Record container image/labels/mounts/restart/port/network; database integrity, table and entity counts, active channel and boolean key equality; current model allowlist; untouched-service image IDs and restart counts; latest.json status and SHA.

- [ ] **Step 3: Stop only New API**

Run:

    docker stop --timeout 30 new-api

Verify it is stopped while all excluded services remain running.

- [ ] **Step 4: Create and verify the cold backup**

Fail if the exact path exists. Resolve source/target under the project root. Copy the complete data/new-api tree; use directory mode 0700 and file mode 0600; write relative-path, size and SHA-256 manifest. Require all hashes, SQLite integrity, channel snapshot and recorded entity counts to match.

Create this mode-0600 rollback override inside the same protected backup directory as compose.rollback.yml, and include it in the backup manifest:

    services:
      new-api:
        image: calciumion/new-api@sha256:428018a37c0b26c163a3367c18401161707cd0e08d0f26a3dde9ff0caa05e34c

Verify with docker compose config that this override changes only the new-api image and preserves every other rendered service field.

- [ ] **Step 5: Recreate only New API**

Run:

    cd /vol1/1000/Solis_Aurora_Gateway
    docker compose up -d --no-deps --force-recreate new-api

Poll conditions for at most 180 seconds. Require running state, TCP 3000 and valid /api/status structure without body output.

- [ ] **Step 6: Verify migration**

Open SQLite read-only. Require integrity, preservation of all pre-upgrade tables and entity counts, active Aurora channel at http://aurora:8080, boolean key equality, at least one eligible token, and expected rc.23 authentication tables.

- [ ] **Step 7: Roll back on a core failure**

Stop/remove only failed rc.23 New API. Move failed data to a protected diagnostic subdirectory inside the upgrade backup. Restore the verified cold copy, then run:

    cd /vol1/1000/Solis_Aurora_Gateway
    docker compose \
      -f docker-compose.yml \
      -f backups/.new-api-rc23-upgrade-20260804/compose.rollback.yml \
      up -d --no-deps --force-recreate new-api

Require the already-present rc.21 digest, database integrity and pre-upgrade entity counts, channel configuration, visible login page and a structurally valid core chat response. Confirm excluded-service image IDs and restart counts remain unchanged. Do not continue after rollback, and do not copy the override into the Git checkout.

---

### Task 8: Verify console and existing chat

**Files:**
- Existing external browser session; no repo writes.

**Interfaces:**
- Consumes: Running rc.23 and existing login state.
- Produces: Console evidence and passed core gate.

- [ ] **Step 1: Connect to the external browser**

Select the connected extension, read complete browser-control documentation, and open the existing New API console or http://192.168.0.38:3000.

If login appears, ask the user to sign in. Never inspect cookies, local storage, saved passwords or session stores.

- [ ] **Step 2: Verify visible state**

Confirm rc.23, one enabled Aurora channel, base http://aurora:8080, key-present boolean, pro/thinking model association and an enabled default client token without opening secret values.

- [ ] **Step 3: Run console channel tests**

Run built-in pro/thinking tests. Record pass/fail, model and bounded duration only. Treat this as supporting evidence.

- [ ] **Step 4: Run the real core health check**

Run:

    cd /vol1/1000/Solis_Aurora_Gateway
    python3 scripts/check_stack_health.py --root /vol1/1000/Solis_Aurora_Gateway --channel-id 1 --json

Require database PASS, exact pro/thinking model range and a structurally valid non-empty completion from at least one model. On a non-UI failure, apply Task 7 rollback.

---

### Task 9: Enable and validate the multimodal matrix

**Files:**
- New API console state.
- Temporary report: /tmp/new-api-multimodal-report.json.

**Interfaces:**
- Consumes: Passed core gate and production probe.
- Produces: Sanitized 14-check report and final passed-only allowlist.

- [ ] **Step 1: Snapshot model, Token and ability scope**

Save a protected, hash-verified pre-change database snapshot inside the existing upgrade backup. Report counts and booleans only.

- [ ] **Step 2: Enable only required models**

Through the console, apply this exact temporary set to channel, selected token and abilities while preserving pro/thinking:

    gpt-4o
    gpt-image-2
    tts-1
    whisper-1

Do not reveal keys.

- [ ] **Step 3: Run one full real matrix**

Run:

    cd /vol1/1000/Solis_Aurora_Gateway
    python3 scripts/new_api_multimodal_probe.py \
      --root /vol1/1000/Solis_Aurora_Gateway \
      --allow-real-api \
      --output /tmp/new-api-multimodal-report.json \
      --json

Require one request per check, zero retries, bounded media and sanitized output.

- [ ] **Step 4: Investigate failures**

For each failed stage collect only:

    token_allows_model
    channel_allows_model
    ability_exists
    new_api_route_selected
    new_api_relay_class
    aurora_route_reached
    aurora_account_available
    proxy_path_expected
    failure_class

failure_class is one of auth, model_scope, route, relay, multipart, sentinel, upstream, timeout, invalid_media, semantic_mismatch or other.

- [ ] **Step 5: Perform one targeted retest after one concrete fix**

Use the console only for a proven model/Token/ability/relay configuration cause. Do not retry unchanged failures or rerun the full matrix.

- [ ] **Step 6: Retain only passed capabilities**

Remove failed or partial models from channel, token and abilities. Native audio translation stays hidden unless it returns English markers. The composed transcription-plus-Chat workflow may be documented even if the native endpoint remains hidden.

- [ ] **Step 7: Remove temporary probe artifacts**

Verify exact /tmp paths before deletion. Do not delete the upgrade backup or rc.21 image.

---

### Task 10: Publish health and record verified facts

**Files:**
- Modify: README.md only if user-visible retained capabilities change.
- Modify: DESIGN.md
- Modify: docs/fnos_deployment.md
- Modify: docs/workbuddy_custom_models.md

**Interfaces:**
- Consumes: Final runtime and matrix evidence.
- Produces: PASS latest.json and a separately gated evidence commit.

- [ ] **Step 1: Publish offline health**

Run:

    cd /vol1/1000/Solis_Aurora_Gateway
    python3 scripts/write_n8n_health_status.py \
      --root /vol1/1000/Solis_Aurora_Gateway \
      --output /vol1/1000/Solis_Studio_OS/data/ops/aurora-gateway/latest.json \
      --channel-id 1

Require five PASS checks, one bounded 0600 rolling file and n8n submount RW=false.

- [ ] **Step 2: Verify untouched services and Git**

Require pre/post image IDs and restart counts for excluded services, four production containers, exact labels/mounts, database integrity, clean FNOS Git, one worktree, diff check, show-ref and fsck.

- [ ] **Step 3: Replace target wording with verified facts**

Document actual rc.23 metadata, migration result, core result, retained multimodal passes, hidden failures and native translation limitation. Never treat a console green indicator as endpoint proof.

- [ ] **Step 4: Run final local verification**

Run full tests, WSL Compose config, Markdown link checks, ignore checks and diff check. Confirm no runtime or protected artifact is tracked.

- [ ] **Step 5: Stop for final documentation commit/push/bundle authorization**

Request explicit authorization for the exact documentation range. Keep the verified backup and rc.21 image until a later cleanup approval.
