# Garmin field audit

What every endpoint returns, what we persist, and what we throw away.

Generated 2026-08-18 by probing each live endpoint and diffing the response
against the fields the pullers map. Regenerate with
`python -m garmin.scripts.manual_audit_fields`.

Two classes of field are deliberately excluded from "worth keeping": identifiers
and privacy/visibility flags (`ownerId`, `*Visibility`, `profileImageUrl*`,
`userRoles`), and UI hints that carry no measurement (`hasVideo`, `favorite`,
`trimmable`). Everything else is listed.

## Summary

| source | fields returned | persisted | worth keeping, not kept |
|---|---|---|---|
| activity summary | 86 | 20 | **21** |
| activity detail | 103 | 22 | **14** |
| training_status | 70 | 32 | **11** |
| training_readiness | 29 | 15 | **9** |
| sleep (stats) | 35 | 22 | **11** |
| weight | 92 | 54 | **6** |
| spo2 | 31 | 5 | **8** |
| daily respiration detail | 26 | 5 | **9** |
| daily HR detail | 13 | 5 | **3** |
| hr_zones | 12 | 12 | 0 |
| steps | 6 | 4 | 2 |
| stress | 6 | 5 | 1 |
| body_battery / heart_rate / hrv / respiration | — | all | 0 |

> Two apparent gaps are false positives: `hrTimeInZone_{n}` and `zone{n}Floor`
> are built with f-strings, so the literal key never appears in source. Both are
> persisted.

## Activity summary — the biggest gap

| field | example | why it matters |
|---|---|---|
| `movingDuration` | 0.0 | Moving vs elapsed time. Needed to test whether Garmin's load uses active time. |
| `elapsedDuration` | 8744.8 | Distinct from `duration`; the three differ on paused activities. |
| **`isManualActivity`** | False | **Flags hand-logged sessions directly.** We currently infer these from null HR. |
| `differenceBodyBattery` | −13 | Body-battery cost of the session. Directly relevant to recovery/sleep modelling. |
| `waterEstimated` | 1484 | Sweat-loss estimate (mL). |
| `bmrCalories` | 237 | Basal component of `calories`, so active calories = calories − bmrCalories. |
| `minHR` | 52 | We keep max and avg but not min. |
| `beginTimestamp` | 1786906399000 | Epoch ms; unambiguous vs the local/GMT string pair. |
| `startTimeGMT` / `endTimeGMT` | 2026-08-16 18:53 | We store local start only. GMT fixes DST/travel ambiguity. |
| `aerobicTrainingEffectMessage` | MINOR_AEROBIC_BENEFIT_0 | Garmin's own label for the aerobic score. |
| `anaerobicTrainingEffectMessage` | MAINTAINING_FAST_FORCE_PRODUCTION_6 | Same for anaerobic. |
| `lapCount` | 1 | Interval structure. |
| `manufacturer` / `deviceId` | GARMIN | Device provenance — the 2022-12-04 watch boundary is currently inferred. |
| `minActivityLapDuration` | 8744.8 | |
| `hasSplits`, `hasPolyline`, `hasHrTimeInZones`, `hasPowerTimeInZones` | | Tell you whether a richer endpoint is worth calling. |
| `splitSummaries.numClimbsCompleted` | 10 | **Climb count for bouldering** — a real volume metric we have nowhere else. |
| `splitSummaries.totalExerciseReps` | 0 | Rep volume at activity level. |
| `splitSummaries.splitType` | CLIMB_ACTIVE | |

## Activity detail (`/activity-service/activity/{id}`)

Mostly overlaps the summary, plus:

| field | example | why |
|---|---|---|
| `summaryDTO.trainingEffect` | 1.40 | Legacy combined score, distinct from the aerobic/anaerobic pair. |
| `metadataDTO.manufacturer` | GARMIN | |
| `metadataDTO.uploadedDate` / `lastUpdateDate` | 2026-08-16T21:19 | Detects retroactive edits to an activity. |
| `metadataDTO.isOriginal` / `trimmed` | True / False | Whether the activity was edited after upload. |
| `metadataDTO.sensors` | None | External sensor list — **would date the bike power meter.** |
| `metadataDTO.hasChartData` | True | |
| `timeZoneUnitDTO.timeZone` | America/Los_Angeles | **Travel detection**, and correct local-time handling. |
| `metadataDTO.eBike*` | None | Irrelevant here but free. |

## training_status

| field | example | why |
|---|---|---|
| `dailyAcuteChronicWorkloadRatio` | 0.2 | The **raw ACWR**. We store `acwrPercent` only. |
| `acwrStatusFeedback` | FEEDBACK_1 | |
| `loadTunnelMin` / `loadTunnelMax` | None | Target load band. |
| `fitnessTrendSport` | NONE | |
| `sinceDate` | 2026-08-16 | How long the current status has held. |
| `recordedDevices.deviceName` | fenix 7 Sapphire Solar | Device provenance again. |
| **`mostRecentVO2Max.heatAltitudeAcclimation.currentAltitude`** | 93 | **Altitude — the camping/elevation signal.** |
| `heatAltitudeAcclimation.altitudeAcclimation` | 0 | |
| `heatAltitudeAcclimation.heatAcclimationPercentage` | 0 | Heat adaptation state. |
| `heatAltitudeAcclimation.previousAltitude` | 0 | |
| `heatAltitudeAcclimation.*AcclimationDate` | 2026-08-17 | |

## training_readiness

`validSleep`, `inputContext`, `timestampLocal`, and seven `*Feedback` strings
(`acwrFactorFeedback`, `hrvFactorFeedback`, `sleepScoreFactorFeedback`,
`sleepHistoryFactorFeedback`, `stressHistoryFactorFeedback`,
`recoveryTimeFactorFeedback`, `recoveryTimeChangePhrase`, `feedbackLong`).

`validSleep` matters most: it says whether the readiness score had a real sleep
input, which is exactly the kind of quiet invalidation that has bitten this
repo before.

## sleep (stats endpoint)

| field | example |
|---|---|
| `avgOvernightHrv` | 58.0 |
| `avgHeartRate` | 47.0 |
| `averageRestingHeartRate` | 45.0 |
| `averageRespiration` | 14.0 |
| `averageBodyBatteryChange` | 60.0 |
| `averageSleepNeed` | 512.0 |
| `averageSleepScore` | 82.0 |
| `averageSleepSeconds` | 26510 |
| **`averageLocalSleepStartTime` / `EndTime`** | −425 / 26820 | Seconds from midnight — **bed/wake timing**, central to sleep work. |
| `averageSkinTempC` / `F` | None | Confirmed dead on this account. |

## spo2 (currently 5 of 31 fields kept)

`avgSleepSpO2`, `avgTomorrowSleepSpO2`, `lowestSpO2`, `latestSpO2`,
`lastSevenDaysAvgSpO2`, plus `sleepStart/EndTimestampGMT|Local` and
`monitoringEnvironmentValues` (an altitude proxy).

## daily respiration detail (5 of 26)

`avgSleepRespirationValue`, `avgWakingRespirationValue`,
`avgTomorrowSleepRespirationValue`, `highestRespirationValue`,
`lowestRespirationValue`, plus the sleep/tomorrow-sleep timestamp quartet.

## daily HR detail

`maxHeartRate`, `minHeartRate`, `lastSevenDaysAvgRestingHeartRate`.

## weight

`visceralFat`, `metabolicAge`, `physiqueRating` are all null on this account
(the Index scale does not report them). Genuinely useful and dropped:
`sourceType` (INDEX_SCALE — distinguishes scale from manual entry),
`timestampGMT` (**time of day of the weigh-in**, which drives much of the
day-to-day variance), `weightDelta`, `trend.weightChange`,
`dailyWeightSummaries.numOfWeightEntries`.

## steps / stress

`totalStepsAverage`, `totalStepsWeeklyAverage` (both derivable), and
`mediumStressDuration` (we keep low and high but not medium).

## Priority

1. **Activity summary** — `movingDuration`, `isManualActivity`,
   `differenceBodyBattery`, `bmrCalories`, `minHR`, GMT timestamps, device.
   Largest gap, and directly blocks the duration question.
2. **`currentAltitude`** from training_status — the elevation signal
   `sleep_analysis` asked for, already in a response we call daily.
3. **Sleep timing** (`averageLocalSleepStartTime/EndTime`) and `avgOvernightHrv`.
4. **`validSleep`** on readiness, as a data-quality gate.
5. Respiration/spo2 sleep-window fields.

---

## What was captured (2026-08-18)

`_session_context_fields` / `_split_summary_fields` in
`garmin/pullers/activities.py`, merged into all three summary pullers, then
backfilled across all 3557 activities. Summaries went 22 → 46 columns.

Captured: `moving_duration_s`, `elapsed_duration_s`, `is_manual`,
`body_battery_change`, `water_estimated_ml`, `bmr_calories`, `steps`,
`lap_count`, `device_id`, `manufacturer`, `start_time_gmt`, `end_time_gmt`,
`begin_timestamp_ms`, `time_zone_id`, `aerobic_te_message`,
`anaerobic_te_message`, `is_personal_record`, `has_splits`, `has_polyline`,
`climbs_completed`, `max_grade`, `split_type`.

A pre-backfill snapshot of the summaries is in
`data/backup/activity_summaries_2026-08-18/`.

### `is_manual` reproduces the inferred hand-logged set exactly

Across 1127 HR-era activities: 96 inferred hand-logged (null `avg_hr`), 96
flagged `is_manual`, **zero disagreements in either direction**. The inference
was right, and is now unnecessary.

### Moving duration does not explain the sublinear duration exponent

Garmin reports moving time for running, cycling, strength, swimming and hiking;
it reports **none at all** for bouldering (0/404) or indoor cycling (0/66), and
none for activities before roughly 2018-2020.

| sport | moving/elapsed | p (elapsed) | p (moving) | Δp |
|---|---|---|---|---|
| strength | 0.346 | 0.742 | 0.784 | +0.043 |
| lap_swimming | 0.749 | 0.730 | 0.777 | +0.047 |
| running | 0.995 | 0.716 | 0.697 | −0.019 |
| cycling | 0.995 | 0.543 | 0.552 | +0.009 |
| hiking | 0.789 | 0.425 | 0.414 | −0.012 |

Even for strength, where moving time is barely a third of elapsed, the exponent
moves 0.74 → 0.78 — nowhere near 1. **Garmin's `training_load` is not computed
on moving time.** The sublinearity is genuine EPOC saturation, now confirmed
against Garmin's own field rather than a heart-rate proxy.

### Still discarded

The remaining priorities from the list above are unimplemented: `currentAltitude`
and the heat/altitude acclimation block on `training_status`; sleep timing
(`averageLocalSleepStartTime`/`EndTime`), `avgOvernightHrv` and
`averageBodyBatteryChange` on the sleep stats endpoint; `validSleep` on
readiness; the respiration and spo2 sleep-window fields; and `timestampGMT` /
`sourceType` on weight.

---

## Round 2 (2026-08-18): health, training, wellness, activity timeseries

### A correction to this document's own method

The summary table above over-counts "discarded". It was built by flattening a
whole response, which conflates two different things: genuine per-day
measurements, and **range aggregates** (`overallStats.*`, `totalAverage.*`,
`aggregations.*`) that describe the requested window rather than a day.
Persisting those would create constant columns, not data.

Probing each puller's *actual per-day record* instead gives a much smaller and
truer gap. `health.py`'s config-driven pullers were nearly complete:

| dataset | fields on the daily record | were mapped | genuinely missing |
|---|---|---|---|
| sleep | 22 | 20 | 2 |
| stress | 5 | 4 | 1 |
| steps | 3 | 3 | 0 |
| heart_rate | 3 | 3 | 0 |
| body_battery | 2 | 2 | 0 |
| hrv | 8 | 7 | 0 (the 8th is the date key) |
| respiration | 3 | 2 | 0 (ditto) |

### Captured

**`health.py`** — `sleep`: `avg_sleep_heart_rate`, `avg_overnight_hrv`.
`stress`: `medium_stress_duration` (low and high were kept, medium was not).
`health_stats`: `source_type`, `timestamp_gmt`, `weight_delta`, plus
`visceral_fat` / `metabolic_age` / `physique_rating` mapped for future scales.
`_post_process_weight` previously used `groupby().mean()`, which silently drops
non-numeric columns — it now averages measurements and takes the day's last
value for everything else, or `source_type` would have vanished on write.

**`training.py`** — the heat/altitude acclimation block (`current_altitude_m`
and 11 others), `acwr_ratio` (the raw ratio, distinct from the stored percent),
`acwr_status_feedback`, `load_tunnel_min`/`max`, `fitness_trend_sport`,
`status_since_date`, `device_name`; and on readiness `valid_sleep`,
`input_context`, `timestamp_local` and seven `*_feedback` strings.

**`health_detailed.py`** — new `pull_daily_summaries()` reads the scalar fields
that arrive alongside the intraday arrays and were previously thrown away,
stored as three new datasets: `wellness_heart_rate`, `wellness_respiration`,
`wellness_spo2`.

**Activity timeseries** — 21 metrics the `/details` endpoint returns and the
puller ignored: `sumMovingDuration` / `sumElapsedDuration` / `sumDuration`,
`directBodyBattery`, `directAvailableStamina` / `directPotentialStamina`,
`directPerformanceCondition`, `directRunCadence`, `directFractionalCadence`,
`directVerticalSpeed`, `sumAccumulatedPower`, `directCaloriesBurnRate`, and the
15-column dual-sided power-meter dynamics block.

### Backfill results

| dataset | rows | columns (before → after) | notable coverage |
|---|---|---|---|
| health_stats | 3908 | 9 → 15 | `source_type` 41%, `visceral_fat` 0% (dead) |
| sleep | 3585 | 22 → 24 | `avg_overnight_hrv` 37%, `avg_sleep_heart_rate` 10% |
| stress | 1355 | 6 → 7 | `medium_stress_duration` 100% |
| training_status | 1354 | 22 → 41 | **`current_altitude_m` 90%** |
| training_readiness | 1350 | 14 → 25 | `valid_sleep` 100% |
| wellness_heart_rate | 1354 | new | `resting_hr_7d_avg` 100%, min/max HR 24% |
| wellness_respiration | 1354 | new | sleep/waking respiration |
| wellness_spo2 | 1354 | new | sleep SpO2, lowest SpO2 |

Snapshots before the writes: `data/backup/daily_2026-08-18/` and
`data/backup/activity_summaries_2026-08-18/`.

### Two negative results worth recording

**`metadataDTO.sensors` is null on every activity tested**, across 2016-2026.
It does *not* date the bike power meter. It can be dated instead from when the
power-dynamics columns (`left_right_balance` and friends) start appearing in the
timeseries.

**`sumMovingDuration` is 0 for bouldering and strength even in the timeseries**,
though the *summary* `movingDuration` is non-zero for strength. Garmin does not
compute per-sample moving time for those sports; the two fields are not the same
quantity and should not be treated as interchangeable.

---

## Canonical sources and duplicate columns (2026-08-18)

Nothing is dropped. Several metrics are genuinely served by more than one Garmin
endpoint, so the curated store now holds the same number under more than one name.
That is fine to keep — it is *not* fine to leave undocumented, because a downstream
model that feeds two aliases in as separate features double-counts one measurement
and reports a confidence it has not earned.

**Rule for consumers: use the canonical column. Treat every alias as the same
measurement, never as an independent one.**

Verified by a pairwise scan of every numeric column across all 14 curated daily
datasets, restricted to pairs with ≥100 overlapping days. "Identical" below means
100.0% of overlapping days are exactly equal, not merely correlated.

### Exact duplicates

| Metric | **Canonical** | Aliases (identical values) | n | Since |
|---|---|---|---|---|
| Overnight HRV, 7-day average | `hrv.weekly_avg` | `training_readiness.hrv_weekly_avg`, `sleep.hrv_7d_average` | 1311–1347 | pre-existing |
| Resting heart rate | `heart_rate.resting_hr` | `sleep.resting_hr`, `wellness_heart_rate.resting_hr` | 1314–1354 | 2 pre-existing, `wellness_*` new |
| Overnight HRV, last night | `hrv.last_night_avg` | `sleep.avg_overnight_hrv` | 1310 | alias new (2026-08-18) |
| Avg sleeping respiration | `respiration.avg_sleep_respiration` | `wellness_respiration.avg_sleep_respiration` | 1312 | alias new |
| Avg waking respiration | `respiration.avg_waking_respiration` | `wellness_respiration.avg_waking_respiration` | 1354 | alias new |
| Daily max HR | `heart_rate.wellness_max_avg_hr` | `wellness_heart_rate.max_hr` | 326 | alias new |
| Daily min HR | `heart_rate.wellness_min_avg_hr` | `wellness_heart_rate.min_hr` | 326 | alias new |

### Near-duplicate (not exact — do not assume interchangeable)

| Metric | Canonical | Near-alias | Agreement |
|---|---|---|---|
| Sleep score | `sleep.sleep_score` | `training_readiness.readiness_sleep_score` | 99.8% identical, r=0.9996 |

The 0.2% disagreement is real, not rounding: readiness is computed at a different
time of day than the sleep summary is finalised. Prefer `sleep.sleep_score`.

### Not duplicates, despite looking like it

- **`sleep.avg_sleep_heart_rate` vs `*.resting_hr`** — r=0.87, mean difference
  **+4.82 bpm**, and **0 of 348** overlapping days are equal. Average HR *across the
  sleep period* is a different quantity from the resting-HR floor. Both are worth
  keeping and they are independent features.
- **Epoch-millisecond timestamp columns** — `health_stats.timestamp_gmt`,
  `hrv.create_time_stamp`, `sleep.gmt_sleep_start_time`, `sleep.gmt_sleep_end_time`
  and friends all show r≈1.0000 against each other while being **0% identical**.
  That correlation is an artifact: any monotonic clock correlates ~1.0 with any
  other. `gmt_sleep_start_time` and `gmt_sleep_end_time` differ on 3585/3585 days.
  A duplicate detector run on this store must exclude epoch columns or it will
  report them; a consumer must not treat them as redundant.

### Three different ACWR-flavoured fields — the easiest thing here to get wrong

All three are named for acute:chronic workload and none of them are the same number.

| Column | Dataset | What it actually is |
|---|---|---|
| `acwr_ratio` | `training_status` | The true acute:chronic workload **ratio** (`dailyAcuteChronicWorkloadRatio`). This is the one that matches the textbook definition. |
| `acwr_percent` | `training_status` | Garmin's capped/rescaled percentage presentation of the same idea. Not `acwr_ratio × 100`. |
| `acwr_factor_pct` | `training_readiness` | An **inverted readiness contribution** — how much load is subtracting from today's readiness score. Higher is *worse*, opposite in sign-sense to the other two. |

Use `acwr_ratio` for load modelling. Use `acwr_factor_pct` only if you are
reconstructing Garmin's readiness score itself.

### Same column name in more than one dataset

Joining these datasets without a suffix will silently collide:

- `resting_hr` — `heart_rate`, `sleep`, `wellness_heart_rate`
- `avg_sleep_respiration` — `respiration`, `wellness_respiration`
- `avg_waking_respiration` — `respiration`, `wellness_respiration`

---

## FIT vs JSON per-point detail — decoded fields (2026-08-18)

Per-activity detail has two sources and `get_activity_detail_timeseries` prefers the
first:

1. **the FIT file** (`get_activity_fit_timeseries`) — 1Hz, e.g. 1408 samples for a
   1403s run, 1560 for a 1550s ride;
2. **the JSON `/details` endpoint** (`get_activity_timeseries`) — ~254 points
   regardless of activity length, decimated for chart rendering.

Both now emit the **same 41 columns in the same order**, so callers can concat them.
That invariant is load-bearing and was briefly broken: new columns were added to the
JSON path only, so a force-backfill re-fetched ~1050 running activities and rewrote
them with the old 15-column schema while reporting success.

### Undocumented FIT record fields, decoded

Garmin ships several fields in the FIT file under `unknown_NNN` names. Verified by
timestamp-joining FIT records against JSON points on running `23528115932` and cycling
`23026068497`:

| Output column | FIT field | Agreement |
|---|---|---|
| `body_battery` | `unknown_143` | 100.0% exact |
| `available_stamina` | `unknown_138` | 100.0% exact |
| `potential_stamina` | `unknown_137` | 100.0% exact |
| `performance_condition` | `unknown_90` | 100.0% exact |
| `grade_adjusted_speed` | `unknown_140` (÷1000) | r = 1.0000 |
| `heart_rate_bpm` | `unknown_136` (= `heart_rate`) | 100.0% exact |

**Two corrections to what this repo previously recorded**, both of the kind a spot
check passes:

- **The stamina pair was swapped.** `available_stamina` is `unknown_138` and
  `potential_stamina` is `unknown_137`, not the reverse. The wrong assignment agrees on
  58–72% of samples. Garmin's invariant `potential >= available` holds for the corrected
  assignment and fails for the old one.
- **`performance_condition` is `unknown_90`** (100.0% exact on both activities). It was
  previously recorded as unconfirmable because the one sample checked was null.

`unknown_87`, `unknown_107`, `unknown_135` remain undecoded.

### `left_right_balance` carries a flag bit

The raw FIT value lands in **136–228**, not 0–100. Masking with `& 0x7F` reproduces
Garmin's `directRightBalance` exactly (100.0% of 195 samples); unmasked it agrees on
**0.0%**. This fails silently — the unmasked number is plausible, just wrong. Anyone
parsing these FIT files directly needs the mask.

Power-phase fields arrive as 2-element `[start, end]` arrays (`left_power_phase`,
`left_power_phase_peak`, and the right-side pair) rather than as separate columns, and
platform-centre offset is `left_pco`/`right_pco`. Torque effectiveness and pedal
smoothness match by name, 100.0% exact.

### What the FIT path cannot supply

Null on FIT-sourced activities, which is nearly all of them:

- `moving_duration_s`, `duration_s` — FIT has no counterpart to Garmin's moving and
  timer clocks, which pause. Deliberately **not** derived from wall-clock time, since
  that is wrong for any paused activity. For activity-level moving duration use the
  **activity-summary** `moving_duration_s`, which is unaffected.
- `vertical_speed` — no FIT counterpart.
- `calories_burn_rate` — dead column, all-null from Garmin on both paths.

`elapsed_duration_s` **is** populated on the FIT path, derived from record timestamps
and verified 100.0% exact against the JSON values.

---

## Endpoints that were never being called (2026-08-19)

The sections above audit *fields within endpoints already called*. This one
covers the other half: whole endpoints Garmin serves that nothing here had ever
requested. Probed live; endpoints that errored or returned nothing for this
account (endurance score, hill score, gear, courses, goals, lactate threshold)
are recorded here as checked-and-absent so they are not re-probed.

| Dataset | Endpoint shape | What it adds |
|---|---|---|
| `race_predictions` | range, **365-day cap** | Predicted 5K/10K/half/marathon times, one row per day. **1354 days back to 2022-11-05.** Computed daily regardless of whether you raced, so it is a genuine longitudinal fitness proxy. |
| `fitness_age` | per-day | Fitness age, chronological age, achievable age, plus the *components* (body fat, RHR, vigorous days/minutes) with each one's target and potential age — i.e. what is holding the number back. |
| `daily_summary` | per-day | Garmin's own end-of-day reconciliation. **94 fields, 83 populated** on a sampled day: energy, steps, floors, intensity minutes, the full stress breakdown, body battery, respiration, SpO2, resting HR. |
| `hydration` | per-day | Intake, goal, and **`sweat_loss_ml`** — Garmin's fluid-loss estimate, which tracks heat stress and activity volume. |
| `intensity_minutes` | per-day | Rolling **weekly** moderate/vigorous totals against the weekly goal. Distinct from the daily-summary fields of similar name. |
| `personal_records` | snapshot | 22 current records with the activity that set each. |
| `devices` | snapshot | 5 registered devices. Useful provenance: `metadataDTO.sensors` is null 2016–2026, so this is the available answer to "which watch recorded this era". |

### The 365-day cap

`racepredictions/daily` returns HTTP 400 for any span over 365 days — verified
by bisection (365 days → 366 rows, 366 days → 400). A whole-history request
fails outright, which is easy to misread as "no data". `_year_chunks` splits
longer spans.

### `daily_summary` overlaps existing datasets — deliberately

Steps, resting HR, stress durations, body battery, respiration and SpO2 all
already have dedicated datasets. `daily_summary` is kept anyway because it is
Garmin's own end-of-day reconciliation and can disagree with the per-metric
endpoints. **Treat the dedicated dataset as canonical and `daily_summary` as a
cross-check**, and do not feed both into one model as independent features.

It also carries `avg_environment_altitude_m` — a second, independent daily
altitude signal alongside `training_status.current_altitude_m`.

---

## Activity types that were being discarded (2026-08-19)

A full `pull_activity_list("2010-01-01", ...)` returned **3838 activities across
27 typeKeys**. The registry claimed 12. **280 activities — 267 hours — were
being pulled from Garmin and then silently dropped** because no dataset claimed
their typeKey.

All 15 missing types are now registered and backfilled:

| typeKey | n | Hours | Through |
|---|---|---|---|
| `treadmill_running` | 108 | 27.4 | 2026-01-06 |
| `indoor_climbing` | 91 | 154.3 | 2026-07-21 |
| `indoor_running` | 18 | 3.2 | 2020-03-13 |
| `multi_sport` | 15 | 28.6 | 2019-04-06 |
| `tennis_v2` | 13 | 13.2 | 2023-02-20 |
| `trail_running` | 9 | 4.3 | 2026-02-06 |
| `transition_v2` | 8 | 0.1 | 2016-04-23 |
| `other` | 6 | 20.4 | 2024-02-10 |
| `paddling_v2`, `volleyball` | 3, 3 | 5.6, 6.5 | 2022, 2023 |
| `swimming` | 2 | 0.9 | 2021-10-18 |
| `softball`, `resort_skiing_snowboarding_ws`, `fitness_equipment`, `walking` | 1 each | ~2.4 | |

### `tennis` was a silent failure, not an omission

The `tennis` dataset **was** registered — but the registry derived the Garmin
typeKey from the dataset name, and Garmin calls it **`tennis_v2`**. So it was
queried for years and always came back empty, which is indistinguishable from
"never played tennis". Nothing errored and nothing logged.

Garmin has versioned several keys this way, so `garmin.datasets.ACTIVITY_TYPE_KEYS`
now holds the mapping explicitly rather than inferring it:

| Dataset | Garmin typeKey |
|---|---|
| `strength` | `strength_training` |
| `tennis` | `tennis_v2` |
| `paddling` | `paddling_v2` |
| `transition` | `transition_v2` |
| `skiing` | `resort_skiing_snowboarding_ws` |

### Running variants are separate datasets on purpose

`treadmill_running`, `indoor_running` and `trail_running` are **not** folded
into `running`. Treadmill pace has no GPS behind it — it is estimated from
cadence — so mixing it in would quietly corrupt pace trends. Trail running
distorts pace through terrain instead. Keeping them separate leaves both
analyzable without contaminating the existing series.

### `multi_sport` does not double-count

Checked before adding: multi_sport rows carry `parentId=None` and their
constituent legs are **not** separately listed (the 147-minute 2019-03-24 entry
has no siblings that day). So these are genuinely missing triathlons, not
duplicates of activities already counted.
