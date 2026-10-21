# Public test data

TrustStack provides a reproducible manifest for three realistic analyst scenarios. The repository does not include publisher documents. Instead, `make public-test-data` downloads each item from its recorded URL, validates it, archives the exact bytes by hash, and enrolls the parsed version for research.

## Bundle

`config/public-test-data.json` defines:

- Microsoft — AI capital investment risks and observable returns
- NVIDIA — Blackwell transition, gross margin, demand, and DeepSeek
- Salesforce — whether Agentforce can support broader portfolio growth

The bundle contains 19 source items across multiple entities and periods.

## Validation

Fixed PDF documents require the expected media type, PDF structure, and exact SHA-256 digest. Changing HTML pages require a valid media type, minimum body size, minimum extracted text, source- and period-specific anchors, and rejection of challenge or error pages.

Validation occurs before bytes are committed to the immutable archive. A failed source remains a recorded failed attempt and does not replace a previously valid version.

## Commands

```sh
make public-test-data
make public-test-data-refresh
```

The first command may reuse the most recent valid archived version. The second attempts a fresh network fetch for every item. Both write a machine-readable report under the selected document root.

## Rights and availability

Each item retains its publisher's copyright. The manifest's rights status is an operational review marker, not permission to redistribute the downloaded document. Do not commit downloaded bytes or mirror them without an independent rights determination.

Availability is not guaranteed. Publishers can change URLs or content, and a network security policy may reject a host if DNS resolution does not produce a public address. Do not disable SSRF or identity checks to force completion.

Preparing source data does not reproduce a frozen report and does not count as model, verification, publication, visual, or user acceptance.
