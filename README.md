# Financial Fraud Detection Engine

Portfolio project 3 of 11. Scores financial transactions for fraud risk in real time (REST and Kafka streaming), explains each score with SHAP, and routes the riskiest activity to human analysts through a case-management console.

> **Status: design phase.** The architecture is in [docs/DESIGN.md](docs/DESIGN.md) and awaiting owner approval. No application code yet.

**Planned stack:** Python · FastAPI · PostgreSQL · Kafka · scikit-learn · XGBoost · SHAP · React + TypeScript · Docker · Terraform (AWS, not deployed)

**Principles**
- Synthetic data only. No real customer or banking information, ever.
- The engine scores *transactions*, not people, and only recommends `allow` or `review`. Humans decide.
- No model metric appears in this repo unless it was produced by the evaluation scripts here.

See [CLAUDE.md](CLAUDE.md) for the engineering standards this project follows.
