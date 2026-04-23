# Extra CA certificates for hAIrspray

This directory is bind-mounted into the container at
`/etc/ssl/hairspray-extra-ca/`. Any `*.crt`, `*.pem`, or `*.cer` file
dropped here is picked up at container boot, concatenated with
certifi's Mozilla trust store, and pointed at as httpx's `verify=`
bundle.

It exists for one reason: making hAIrspray work correctly behind
**SASE fabrics and NGFWs that decrypt-and-re-sign HTTPS** — Cato,
Zscaler, Palo Alto Prisma, Netskope, Check Point Harmony, Fortinet
FortiGate with SSL inspection, etc. Without the fabric's re-sign CA
installed, every outbound TLS handshake fails with
`CERTIFICATE_VERIFY_FAILED` against an untrusted issuer (which is
exactly the expected behavior — the fabric presented a cert you
shouldn't trust by default).

## How to install your fabric's CA

1. Export the re-sign root CA from your fabric's management plane.
   For Cato: **System → Certificate → Export CA** (exact path varies
   by Cato portal version).

2. Save the exported cert file into this directory. It must be in
   PEM format (human-readable, starts with `-----BEGIN CERTIFICATE-----`)
   and end with `.crt`, `.pem`, or `.cer`. Filename otherwise is
   arbitrary.

   ```bash
   cp ~/Downloads/cato-root-ca.crt /opt/ai-spray/certs/
   ```

3. Rebuild the container:

   ```bash
   docker compose down
   docker compose build --no-cache
   docker compose up -d
   ```

4. Verify the boot log shows the custom-bundle mode is active:

   ```bash
   docker compose logs hairspray 2>&1 | grep -E 'tls_custom_ca|tls_system_verify|tls_verify_disabled' | head
   ```

   You should see something like:

   ```json
   {"mode": "custom-bundle",
    "extras": ["cato-root-ca.crt"],
    "bundle_path": "/tmp/hairspray-ca-bundle.pem",
    "event": "tls_custom_ca"}
   ```

   If you see `tls_verify_disabled` instead, the cert file wasn't
   picked up. Check that the filename ends in `.crt` / `.pem` / `.cer`
   and that the file is readable by UID 10001 (`simulator` inside the
   container).

## What gets tracked in git

The directory itself is tracked (via this README and the bundled
`.gitignore`). The actual CA cert files you drop here are **not** —
`.gitignore` in this directory ignores everything except itself and
this README, so there's no risk of accidentally pushing a tenant-
specific CA to a public repo.

## Multiple CAs

You can drop more than one file in here — e.g. if you have a test
fabric and a production fabric whose re-sign CAs differ. All of them
get appended to the combined bundle.
