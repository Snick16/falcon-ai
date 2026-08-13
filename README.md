# Falcon AI Hunter

Falcon AI Hunter scans Solana token opportunities, rates each candidate with the existing Falcon scoring logic, and renders ranked results in the Streamlit dashboard.

## Falcon Surge Detection

Falcon Surge is an additive subsystem that tracks scan-over-scan acceleration and classifies each token as:

- WATCH: market cap acceleration at or above the WATCH threshold with positive buy pressure
- SURGE: stronger market-cap acceleration plus clear volume acceleration and buys greater than sells
- BREAKOUT: highest acceleration tier with stronger volume acceleration, stronger buy pressure, and a higher liquidity floor

Surge detection does not require Smart Money, KOL, Telegram, or X confirmation to alert. Confirmations raise confidence when available but do not block surge signals.

Telegram sends dedicated alerts when a token first reaches SURGE or BREAKOUT. Duplicate alerts at the same level are suppressed, while level progression (SURGE to BREAKOUT) is allowed.

## Supported Discovery Sources

- dexscreener_latest: public DexScreener latest boosts endpoint
- dexscreener_boosted: public DexScreener top boosts endpoint
- dexscreener_trending: public DexScreener search-based trending flow
- new_solana_pairs: public DexScreener latest token profiles flow
- pumpfun_tokens: optional source, enabled with PUMPFUN_ENABLED using a public Pump.fun endpoint
- telegram_channels: optional source, enabled with TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION, TELEGRAM_CHANNELS
- x_social: optional source, enabled with X_BEARER_TOKEN and X_ACCOUNTS

## Environment Variables

Copy .env.example into .env and populate only what you need.

Required for Telegram alert sending:

- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

Optional scanner/social variables:

- TELEGRAM_API_ID
- TELEGRAM_API_HASH
- TELEGRAM_SESSION
- TELEGRAM_CHANNELS
- TELEGRAM_LOOKBACK_MINUTES
- TELEGRAM_MAX_MESSAGES_PER_CHANNEL
- X_BEARER_TOKEN
- X_ACCOUNTS
- X_API_URL
- PUMPFUN_ENABLED
- PUMPFUN_MAX_TOKENS
- PUMPFUN_LOOKBACK_MINUTES
- FALCON_X_API_URL
- FALCON_TELEGRAM_API_URL

Alert-engine controls:

- FALCON_ALERTS_ENABLED
- FALCON_ALERT_DRY_RUN
- FALCON_ALERT_ONE_TIME_PER_CONTRACT
- FALCON_ALERT_COOLDOWN_MINUTES
- FALCON_ALERT_MIN_SCORE
- FALCON_ALERT_MIN_CONFIDENCE
- FALCON_ALERT_MIN_LIQUIDITY_USD
- FALCON_ALERT_MIN_5M_CHANGE_PCT
- FALCON_ALERT_MIN_BUY_SELL_RATIO
- FALCON_ALERT_MIN_BUYS_5M
- FALCON_ALERT_MAX_RISK_RANK
- FALCON_ALERT_ALLOWED_MOMENTUM

Surge detector controls:

- FALCON_SURGE_ENABLED
- FALCON_SURGE_MIN_MC_USD
- FALCON_SURGE_MAX_MC_USD
- FALCON_SURGE_MIN_LIQ_USD
- FALCON_SURGE_BREAKOUT_MIN_LIQ_USD
- FALCON_SURGE_WATCH_MC_CHANGE_PCT
- FALCON_SURGE_SURGE_MC_CHANGE_PCT
- FALCON_SURGE_BREAKOUT_MC_CHANGE_PCT
- FALCON_SURGE_MIN_VOLUME_ACCEL
- FALCON_SURGE_BREAKOUT_MIN_VOLUME_ACCEL
- FALCON_SURGE_BREAKOUT_MIN_BUY_PRESSURE
- FALCON_SURGE_FOCUS_NEAR_500K
- FALCON_SURGE_FOCUS_500K
- FALCON_SURGE_FOCUS_1M
- FALCON_SURGE_ALERTS_ENABLED
- FALCON_SURGE_ALERT_DRY_RUN
- FALCON_SURGE_ALERT_COOLDOWN_MINUTES
- FALCON_SURGE_ALERT_RESET_MINUTES

## Which Scanners Work Without Credentials

No credentials required:

- dexscreener_latest
- dexscreener_boosted
- dexscreener_trending
- new_solana_pairs

Credentials/config required:

- pumpfun_tokens
- telegram_channels
- x_social

When optional credentials are missing, the scanner returns no results for that source and a clear status message in scanner_status. It does not fabricate data.

## Run

Install dependencies:

python -m pip install -r requirements.txt

Start dashboard:

python -m streamlit run dashboard.py

Run CLI scanner:

python Scanner.py

Create Telegram session for channel scanning (local one-time setup):

python telegram_setup.py

## Smoke Test

Run:

python smoke_test.py

python -m unittest -v test_telegram_parser.py

python -m unittest -v test_pumpfun_parser.py

python -m unittest -v test_x_parser.py

The smoke test checks:

- project modules compile
- DexScreener latest scan returns structured candidates
- multi-source duplicate merge by token address works
- missing social credentials do not crash collection
- scanner status includes not-configured source messages
- Telegram parser extracts CA/contract/mint/address calls from sample messages without live Telegram login
- Pump.fun parser normalizes and deduplicates sample mint payload rows without live network dependency
- X parser extracts Solana contract mentions from posts and links, merging duplicate mentions across authors without live network dependency
