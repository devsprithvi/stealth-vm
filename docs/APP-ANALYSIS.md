# Static analysis of third-party apps before testing them in the sandbox

The user's working method: test ordinary software (Electron apps, installers,
tools) in `svm` before ever running a malware sample, and don't assume a
sandbox tell is a hardware/VM-detection problem just because an app reacted
badly. Read what the app actually checks first. This doc is the repeatable
recipe used for the first case (Qoder, 3 Nov 2026) and the reusable tool it
produced.

## Recipe

1. **Get the installer without guessing download buttons.** Vendor download
   pages are usually client-rendered; the real CDN URL is normally embedded as
   a plain string literal in one of the page's JS chunks.
   ```bash
   curl -s -A "Mozilla/5.0" https://vendor.example/download -o page.html
   grep -oE 'https://[a-zA-Z0-9_./-]+/_next/static/chunks/[a-zA-Z0-9._-]+\.js' page.html | sort -u > chunks.txt
   while read -r u; do curl -s "$u" -o "$(basename "$u")"; done < chunks.txt
   grep -ohE '"https://[^"]*\.(exe|dmg|deb|rpm|AppImage|zip)[^"]*"' *.js | sort -u
   ```
   This is how `https://download.qoder.com/qoder-app/releases/latest/Qoder-win-x64-user.exe`
   and the `.deb` build were found after the obvious `download.qoder.com/<name>.exe`
   guesses all 404'd; the real bucket key prefix was `qoder-app/releases/latest/`.

2. **Extract without installing.** NSIS installers: `7z x installer.exe`, then
   `7z x "$PLUGINSDIR/app-64.7z"` for the payload. `.deb`: `dpkg-deb -x pkg.deb dir`.
   Either way you land on `resources/app.asar`.

3. **Extract the asar without Node.** `scripts/asar-extract.py extract app.asar out/`
   or `... grep app.asar needle1 needle2` for a quick substring scan. It is a
   ~90-line dependency-free parser (asar is just
   `[Chromium Pickle framing][JSON header][concatenated file bytes]`; the
   header is plain ASCII JSON, found by scanning for the first `{` and letting
   `json.JSONDecoder.raw_decode` report where it ends). No `npm install asar`,
   no Electron, no Node required for this step.

4. **Grep the real source first.** Most of an asar's *files* are vendored
   `node_modules`; search the app's own `out/main/*.js` / `electronMain.js`
   first; broad greps across everything mostly surface unrelated library code.

5. **If the code is obviously minified, that's normal — bundlers do that.**
   If grepping for plain-text vendor/VM strings (`vmware`, `virtualbox`, `qemu`,
   `hypervisor`, `wmic`, `webgl`, `canvas`, `fingerprint`, ...) finds nothing in
   the app's own files but the file is large and unreadable, check whether it's
   *deliberately* obfuscated beyond normal minification before concluding
   "it doesn't check for that". The tell: calls like `e(12345,6789)` everywhere
   instead of real property/string names, and a function that looks like
   `function Q(t,x){t=t-N; const e=J_(); let f=e[t]; ...base64-decode f...}`
   plus a one-time bootstrap `(function(t,x){ ... e.push(e.shift()) ... })(J_, SOME_NUMBER)`
   near the top of the file. This is the well-known
   [javascript-obfuscator.io](https://github.com/javascript-obfuscator/javascript-obfuscator)
   string-array pattern: a big array of base64 string literals, rotated at
   load time until a checksum matches, then indexed with a constant offset
   scattered across many tiny per-scope wrapper functions.

6. **Full deobfuscation is expensive — try it, but budget memory and bail out.**
   `npm install -g webcrack` (or `npx webcrack`) handles this pattern properly
   via AST transforms. On this host (15 GiB RAM) it OOM'd on a 1.9 MB minified
   file at default settings, and with `--no-unpack --no-unminify` and an 8000 MB
   heap it still reached 5+ GB RSS before being stopped as a precaution — **do
   not run it while any guest is running or while host free memory is already
   low**; check `free -m` first, and stop it if resident memory climbs past a
   couple GB with little free memory left.

7. **The cheap alternative that doesn't need full deobfuscation: dump the whole
   string pool.** You don't need to know *which* call site decodes to which
   string to answer "does this app's vocabulary include X anywhere" — only
   that the string exists in the array at all. Extract the array literal
   (balanced-bracket scan from `function J_(){const t=[` to the matching `]`),
   write it as an ES module default export, and decode every entry with a
   small Node script using the *exact* decode function copied verbatim out of
   the target file (transcribing it by hand is error-prone — one dropped
   operator and every string comes out as garbage; copy the literal function
   text with a regex and `eval()` it rather than retyping it). Then grep the
   decoded pool. This is a few seconds of CPU and a few MB of RAM, regardless
   of how large the bundle is, because it skips the AST/control-flow work
   entirely — it only answers "does this string exist", not "where is it used".

## Finding: Qoder (3 Nov 2026)

Static analysis of `Qoder-win-x64-user.exe` (NSIS) and `Qoder-linux-amd64.deb`,
both version resolved via `download.qoder.com/qoder-app/releases/latest/`.
`resources/app.asar`'s main-process bundle (`out/main/electronMain.js`) uses
the javascript-obfuscator.io string-array pattern described above with 57,810
pooled strings. The full pool was decoded and grepped (see recipe step 7):

- **Zero hits** for `vmware`, `virtualbox`, `vbox`, `hyper-v`, `qemu`, `kvm`,
  `xen`, `parallels`, `sandboxie`, `bochs`, `seabios`, `ovmf`, `tianocore`,
  anywhere in the 57,810-string pool (main process + all bundled
  `node_modules`). **Zero hits** for `webgl`, `getGPUInfo`, canvas/GPU
  fingerprinting terms.
- All 11 hits for `sandbox` are Electron/Chromium's own process sandbox
  (`--no-sandbox`, `--disable-gpu-sandbox`, iframe `sandbox` attributes) —
  unrelated to detecting a VM.
- **What Qoder actually persists:** a local SQLite table,
  `local_device_identity(singleton_key, device_id, initialized_at)`, written
  once (`INSERT ... VALUES (1,?,?)`) and read back by
  `SELECT device_id FROM local_device_identity WHERE singleton_key=1`. This
  backs a `getMachineIdentity()` call in the main process that is sent to the
  login/auth flow (`{challenge, nonce, machineId}` → login URL) and attached
  to internal session/sync RPCs. It is Qoder's own app-data file, separate
  from the OS-level identifiers (Windows MachineGuid/SID, Linux
  `/etc/machine-id`) that `svm`'s freshness work already regenerates per
  session — and since it lives under the app's user-data directory (inside
  the session overlay, not the sealed base), a disposable session should get
  a fresh one automatically, the same way it gets a fresh user profile.
- There is also an `account-fingerprint-salt` / `fingerprintSaltPath` and a
  certificate-fingerprint system for Qoder's own multi-device pairing feature;
  none of it is browser/hardware fingerprinting (canvas, WebGL, audio, fonts).
  All other `fingerprint` hits are content hashes for chat-message dedup.

**Conclusion: nothing found here points at hardware/firmware/CPUID VM
detection.** The identity work `svm` already does (SMBIOS, CPU, MAC, serials)
is very unlikely to be what Qoder reacted to. This shifts weight toward
`docs/CHECKPOINT.md` section 7's hypotheses 1 (WARP network reputation) and 5
(account-side state/abuse detection tied to the Qoder account or its
`account-fingerprint-salt`), not hypothesis 3 (GPU) or 4 (timing). It does not
rule those out for *other* software — this finding is specific to Qoder;
re-run the recipe per app.
