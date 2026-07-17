#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

START=""
END=""
TIMEZONE="MST"
PROFILE=""
STATS_PROFILE="pde"
SUMMARY_PROFILE="pde"
STATS_QUERY=""
SUMMARY_QUERY='responders:"PDE"'
OUTPUT_DIR="$PROJECT_ROOT/out"
OUTPUT_BASENAME="pde_alerts"
PYTHON_BIN="python3"
SKIP_HISTORY_REFRESH="false"

if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
fi

usage() {
  cat <<'EOF'
Run the full PDE reporting pipeline in one command.

Usage:
  ./api/jsmreport/run_reporting_pipeline.sh --start YYYY-MM-DDTHH:MM --end YYYY-MM-DDTHH:MM [options]

Required:
  --start DATETIME         Window start in local timezone (example: 2026-06-08T09:00)
  --end DATETIME           Window end in local timezone (example: 2026-07-02T09:00)

Options:
  --timezone TZ            Window timezone, default: MST (MST or UTC)
  --profile NAME           Legacy shortcut: sets both stats and summary profiles
  --stats-profile NAME     Stats export profile, default: pde
  --summary-profile NAME   Summary export profile, default: pde
  --stats-query QUERY      Optional explicit query for stats export (overrides --stats-profile)
  --summary-query QUERY    Optional explicit query for summary export (overrides --summary-profile)
  --output-dir PATH        Output folder, default: ./out
  --basename NAME          Base output name, default: pde_alerts
  --skip-history-refresh   Skip step 1 history snapshot delta refresh
  -h, --help               Show this help

Outputs:
  <output-dir>/<basename>_<start>_to_<end>_<tz>_stats_lifecycle.csv
  <output-dir>/<basename>_<start>_to_<end>_<tz>_summary_source.csv
  <output-dir>/<basename>_summary.json
  <output-dir>/<basename>_summary.html
  <output-dir>/<basename>_summary.txt
EOF
}

sanitize_for_name() {
  echo "$1" | tr ' :' '__'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)
      START="${2:-}"
      shift 2
      ;;
    --end)
      END="${2:-}"
      shift 2
      ;;
    --timezone)
      TIMEZONE="${2:-}"
      shift 2
      ;;
    --profile)
      PROFILE="${2:-}"
      shift 2
      ;;
    --stats-profile)
      STATS_PROFILE="${2:-}"
      shift 2
      ;;
    --summary-profile)
      SUMMARY_PROFILE="${2:-}"
      shift 2
      ;;
    --stats-query)
      STATS_QUERY="${2:-}"
      shift 2
      ;;
    --summary-query)
      SUMMARY_QUERY="${2:-}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --basename)
      OUTPUT_BASENAME="${2:-}"
      shift 2
      ;;
    --skip-history-refresh)
      SKIP_HISTORY_REFRESH="true"
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$START" || -z "$END" ]]; then
  echo "Both --start and --end are required." >&2
  usage
  exit 1
fi

sanitize_step_name() {
  echo "$1" | tr ' /[]:' '_____' | tr -cd '[:alnum:]_\n'
}

run_logged_step() {
  local step_label="$1"
  local artifact_path="$2"
  local log_file="$3"
  shift 3
  local cmd=("$@")

  {
    echo "Step: $step_label"
    echo "Artifact target: ${artifact_path:-<none>}"
    printf "Command:"
    for token in "${cmd[@]}"; do
      printf " %q" "$token"
    done
    printf "\n"
  } >"$log_file"

  local exit_code=0
  if "${cmd[@]}" >>"$log_file" 2>&1; then
    exit_code=0
  else
    exit_code=$?
  fi

  local artifact_exists="no"
  local artifact_size="0"
  if [[ -n "$artifact_path" && -f "$artifact_path" ]]; then
    artifact_exists="yes"
    artifact_size="$(wc -c < "$artifact_path" | tr -d '[:space:]')"
  fi

  echo "STEP_RESULT|step=$step_label|exit=$exit_code|log=$log_file|artifact=$artifact_path|artifact_exists=$artifact_exists|artifact_size_bytes=$artifact_size"

  if [[ "$exit_code" -ne 0 ]]; then
    echo "Step failed: $step_label"
    echo "Log: $log_file"
    tail -n 40 "$log_file" || true
    return "$exit_code"
  fi
}

mkdir -p "$OUTPUT_DIR"

if [[ -n "$PROFILE" ]]; then
  STATS_PROFILE="$PROFILE"
  SUMMARY_PROFILE="$PROFILE"
fi

safe_start="$(sanitize_for_name "$START")"
safe_end="$(sanitize_for_name "$END")"
tz_lower="$(echo "$TIMEZONE" | tr '[:upper:]' '[:lower:]')"

STATS_CSV_PATH="$OUTPUT_DIR/${OUTPUT_BASENAME}_${safe_start}_to_${safe_end}_${tz_lower}_stats_lifecycle.csv"
SUMMARY_CSV_PATH="$OUTPUT_DIR/${OUTPUT_BASENAME}_${safe_start}_to_${safe_end}_${tz_lower}_summary_source.csv"
JSON_PATH="$OUTPUT_DIR/${OUTPUT_BASENAME}_summary.json"
HTML_PATH="$OUTPUT_DIR/${OUTPUT_BASENAME}_summary.html"
TXT_PATH="$OUTPUT_DIR/${OUTPUT_BASENAME}_summary.txt"

RUN_STAMP="$(date +%Y%m%d_%H%M%S)"
PIPELINE_LOG_DIR="$OUTPUT_DIR/pipeline_logs/$RUN_STAMP"
mkdir -p "$PIPELINE_LOG_DIR"

if [[ "$SKIP_HISTORY_REFRESH" == "true" ]]; then
  echo "[1/6] Skipping alert history snapshot refresh (--skip-history-refresh)"
  echo "STEP_RESULT|step=[1/6] history-refresh|exit=0|log=<skipped>|artifact=$PROJECT_ROOT/data/alert_history_snapshot.json|artifact_exists=yes|artifact_size_bytes=$(wc -c < "$PROJECT_ROOT/data/alert_history_snapshot.json" | tr -d '[:space:]')"
else
  step_label="[1/6] history-refresh"
  step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
  echo "[1/6] Refreshing alert history snapshot delta (through $END $TIMEZONE)"
  run_logged_step "$step_label" "$PROJECT_ROOT/data/alert_history_snapshot.json" "$step_log" \
    "$PYTHON_BIN" "$PROJECT_ROOT/api/jsmreport/update_alert_history_snapshot.py" \
    --snapshot "$PROJECT_ROOT/data/alert_history_snapshot.json" \
    --through "$END" \
    --timezone "$TIMEZONE"
fi

echo "[2/6] Exporting stats CSV -> $STATS_CSV_PATH"
stats_export_args=(
  export-csv
  --output "$STATS_CSV_PATH"
  --start "$START"
  --end "$END"
  --timezone "$TIMEZONE"
)
if [[ -n "$STATS_QUERY" ]]; then
  stats_export_args+=(--query "$STATS_QUERY")
else
  stats_export_args+=(--profile "$STATS_PROFILE")
fi
step_label="[2/6] export-stats-csv"
step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
run_logged_step "$step_label" "$STATS_CSV_PATH" "$step_log" \
  "$PYTHON_BIN" -m cli "${stats_export_args[@]}"

echo "[3/6] Exporting summary source CSV -> $SUMMARY_CSV_PATH"
summary_export_args=(
  export-csv
  --output "$SUMMARY_CSV_PATH"
  --start "$START"
  --end "$END"
  --timezone "$TIMEZONE"
)
if [[ -n "$SUMMARY_QUERY" ]]; then
  summary_export_args+=(--query "$SUMMARY_QUERY")
else
  summary_export_args+=(--profile "$SUMMARY_PROFILE")
fi
step_label="[3/6] export-summary-source-csv"
step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
run_logged_step "$step_label" "$SUMMARY_CSV_PATH" "$step_log" \
  "$PYTHON_BIN" -m cli "${summary_export_args[@]}"

echo "[4/6] Building JSON summary -> $JSON_PATH"
step_label="[4/6] build-json-summary"
step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
reporter_json_cmd=()
reporter_json_cmd+=(
  "$PYTHON_BIN" -m cli.reporter
  --json
  --output "$JSON_PATH"
  summarize-csv
  --input "$STATS_CSV_PATH"
  --summary-input "$SUMMARY_CSV_PATH"
)
run_logged_step "$step_label" "$JSON_PATH" "$step_log" "${reporter_json_cmd[@]}"

echo "[5/6] Building HTML summary -> $HTML_PATH"
step_label="[5/6] build-html-summary"
step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
reporter_html_cmd=()
reporter_html_cmd+=(
  "$PYTHON_BIN" -m cli.reporter
  --format html
  --output "$HTML_PATH"
  summarize-csv
  --input "$STATS_CSV_PATH"
  --summary-input "$SUMMARY_CSV_PATH"
)
run_logged_step "$step_label" "$HTML_PATH" "$step_log" "${reporter_html_cmd[@]}"

echo "[6/6] Building text summary -> $TXT_PATH"
step_label="[6/6] build-text-summary"
step_log="$PIPELINE_LOG_DIR/$(sanitize_step_name "$step_label").log"
run_logged_step "$step_label" "$TXT_PATH" "$step_log" \
  "$PYTHON_BIN" -m cli.reporter \
  --output "$TXT_PATH" \
  summarize-csv \
  --input "$STATS_CSV_PATH" \
  --summary-input "$SUMMARY_CSV_PATH"

echo "Done. Artifacts:"
echo "  Stats CSV:   $STATS_CSV_PATH"
echo "  Summary CSV: $SUMMARY_CSV_PATH"
echo "  JSON: $JSON_PATH"
echo "  HTML: $HTML_PATH"
echo "  TXT:  $TXT_PATH"
echo "  Step logs: $PIPELINE_LOG_DIR"
