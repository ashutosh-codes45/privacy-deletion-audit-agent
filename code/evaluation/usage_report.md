# Usage Report — full_dataset_run

- Run id: `60d035c385e4`
- Status: **COMPLETE**
- Started (UTC): 2026-10-25T03:57:45Z
- Finished (UTC): 2026-10-25T03:57:49Z
- Wall-clock duration: 3s
- Items audited: 518 of 518 expected
- Items succeeded: 518
- Items failed: 0
- Model calls: 0
- Currency: USD

## Latency per audited item

| Metric | Value |
| --- | --- |
| Items measured | 518 |
| Total audit time | 2.779 s |
| Average latency per item | 0.005 s |
| Median (p50) | 0.004 s |
| p90 | 0.010 s |
| p95 | 0.011 s |
| Fastest item | 0.000 s |
| Slowest item | 0.032 s |
| Throughput (mean) | 186.41 items/s |

## Token and cost usage by model

_No model calls were recorded during this sweep._

## Overall totals

| Metric | Value |
| --- | --- |
| Model calls | 0 |
| Model calls per request | 0.00 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0.0 |
| Estimated total cost | $0.0000 USD |
| Estimated cost per request | $0.000000 USD |

## Pricing constraints applied

| Model | Input (USD / 1M tokens) | Output (USD / 1M tokens) | Verified |
| --- | ---: | ---: | --- |

_All models were charged at verified rates._

## Reproduce

```text
python code/main.py            # audits dataset/deletion_requests.csv -> output.csv
python -m evaluation.metrics_tracker --demo   # render this layout without a run
```

## Evidence-extraction token allocation

The deterministic pass reads every text, SQL, JSON and PDF asset locally (standard library only). Screenshot assets stay unresolved unless a vision provider is configured, so no provider call is made and nothing is billed.

| Metric | Value |
| --- | ---: |
| Evidence assets indexed (text/PDF/SQL/JSON) | 83 |
| Evidence assets skipped (image, no provider) | 11 |
| Local extraction cache hits | 15 |
| Assets analysed this run | 0 |
| Provider (vision) model calls | 0 |
| Extraction prompt tokens (allocation) | 1,143 |
| Extraction output tokens (allocation) | 0 |
| Total extraction tokens (allocation) | 1,143 |
| Average tokens per request | 2.2 |
| Requests in this sweep | 518 |

### Model providers and pricing constraints applied

| Provider | Model | Input (USD / 1M tokens) | Output (USD / 1M tokens) | Verified |
| --- | --- | ---: | ---: | --- |
| deepseek | DeepSeek-V4.1-Flash | 0.1400 | 0.2800 | yes |

### Explicit cost calculation (verified rates)

Rates applied for `DeepSeek-V4.1-Flash` (provider `deepseek`, verified):

* input  - 0.1400 USD per 1,000,000 tokens
* output - 0.2800 USD per 1,000,000 tokens

```text
input cost  = 1,143 / 1,000,000 x 0.1400 = $0.000160
output cost = 0 / 1,000,000 x 0.2800 = $0.000000
total       = $0.000160 over 518 requests
average     = $0.000000 per request
```

Because the production run makes zero provider calls, the billed model cost is $0.000000; the figures above are the token budget the extraction hook would consume once a provider credential is configured.
