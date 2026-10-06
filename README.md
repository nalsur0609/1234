# 1234

Public CI compute carrier.

Purpose: execute bounded, non-sensitive CI workloads on public GitHub-hosted runners when private project Actions minutes are unavailable.

## Governance

- Private project repositories remain the only project SSOT.
- This repository has no product, architecture, acceptance, merge, or STABLE authority.
- Never commit secrets, tokens, .env files, customer data, commercial/private supplier data, private Vendor Packs, or other sensitive material.
- Every workload must be bound to exact source identities / commit SHAs / hashes from its originating private Issue/PR.
- Return factual run/job IDs, results, counts, hashes, and evidence to the originating private Issue/PR.
- Avoid retaining large GitHub Actions artifacts unless explicitly required.
- Prefer: execute -> verify -> log SHA/bytes/results -> private handoff.
