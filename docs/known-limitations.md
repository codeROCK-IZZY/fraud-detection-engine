# Known limitations

Honest list of what this project does not do, or does only partly.

## Data and model
- **Synthetic data only.** Metrics describe how learnable the simulator is, not real-world performance. Real fraud is more varied, adaptive and noisier.
- **Selection bias in analyst labels.** Analysts only see flagged transactions. The random review sample (`review_sample_rate`, default 0.1%) reduces but does not remove this.
- **Hidden positives.** ~10% of simulated fraud is never reported, so it is trained on as legitimate.
- **No account-event feed.** Account takeover is detected from device, payee and IP signals; there is no "password changed / new phone number" input.
- **No cross-card graph features.** "Cards seen on this device in the last 24 h" would catch fraud rings well; not built.
- **USD only**, single currency.
- **No PyTorch autoencoder.** The design allowed an optional autoencoder comparison; only Isolation Forest is built and evaluated.
- **UTC time of day.** Customer time zones are not modelled; "time of day" features are relative to each card's own history, which limits the damage.

## Engineering
- **Rate limiting and model cache are per process.** With several API replicas each enforces its own login limit (same as the AI Predictive Maintenance and Intelligent Document AI platforms).
- **Merchant fraud-rate lookups** count rows per scored transaction. Fine at demo scale; at high volume a per-merchant counter table would be needed.
- **Backfill explanations**: transactions scored in bulk get reason sentences only when medium or above.
- **Kafka is not secured locally** (plaintext on the Docker network). On AWS it would need TLS + IAM (MSK), which is not provisioned.
- **Streaming is disabled on AWS** in the Terraform (MSK cost). REST and CSV ingestion work there.
- **S3 artifact/upload storage** is not exercised by automated tests (no AWS in CI).
- **No "hold" action.** By design the engine is advisory (`allow` / `review`) and never declines payments.

## Not built
- Multi-currency, multi-tenant organisations, real card-network integration, KYC/identity checks, mobile app.
