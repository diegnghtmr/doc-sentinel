#!/bin/sh
# Offline, read-only Docker launcher for Linux and macOS. No eval or shell-built commands.

usage() {
    printf '%s\n' \
        'usage: doc-sentinel-docker.sh [--rebuild] {clean,detect,sanitize,review-merge,version,intent} ...' \
        '  clean FILE [-o PATH | --output-dir DIR] [--force-output] [--json|--agent-summary] [--report PATH]' \
        '  detect FILE [--sanitize [-o PATH | --output-dir DIR] [--force-output]] [--agent-summary] [--review-dir DIR]' \
        '  sanitize FILE --review-dir DIR [-o PATH | --output-dir DIR] [--force-output] [--report PATH] [--agent-summary]' \
        '  review-merge --review-dir DIR [--report PATH] [--agent-summary]'  \
        '  version | intent TEXT...' \
        '  update and --allow-network require the Python CLI on the host.'
}
fail() { printf '%s\n' "$1" >&2; exit 2; }

# Resolve an existing file or an existing parent plus a new filename, including symlinks.
resolve_path() {
    if [ -e "$1" ] || [ -L "$1" ]; then
        realpath "$1"
    else
        rp_parent=$(realpath "$(dirname "$1")") || return 1
        printf '%s/%s\n' "${rp_parent%/}" "$(basename "$1")"
    fi
}
same_path() {
    [ "$1" = "$2" ] || { [ -e "$1" ] && [ -e "$2" ] && [ "$1" -ef "$2" ]; }
}

rebuild=0
if [ "${1-}" = '--rebuild' ]; then rebuild=1; shift; fi
[ "$#" -gt 0 ] || { usage; exit 2; }
case "$1" in
    --help|-h) usage; exit 0 ;;
    update) fail 'update runs on a git checkout with network: use the Python CLI on the host (doc-sentinel update)' ;;
    clean|detect|sanitize|review-merge|version|intent) mode=$1; shift ;;
    *) fail "unknown command: $1" ;;
esac

input=''; output=''; output_dir=''; report=''; review=''; json=0; agent=0; force=0; prompt_guard=0; fail_on=''; sanitized=0; default_clean=0
[ "$mode" != sanitize ] || sanitized=1
if [ "$mode" = clean ] || [ "$mode" = detect ] || [ "$mode" = sanitize ] || [ "$mode" = review-merge ]; then
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --allow-network|--allow-network=*) fail '--allow-network is not available in the container launcher' ;;
            -o|--output|--output-dir|--report|--fail-on|--review-dir)
                option=$1; shift
                [ "$#" -gt 0 ] && [ -n "$1" ] || fail "missing value for $option"
                case "$option" in
                    -o|--output) [ "$mode" != review-merge ] || fail "unknown option: $option"; output=$1 ;;
                    --output-dir) [ "$mode" != review-merge ] || fail "unknown option: $option"; output_dir=$1 ;;
                    --report) report=$1 ;;
                    --review-dir) [ "$mode" != clean ] || fail "unknown option: $option"; review=$1 ;;
                    --fail-on) [ "$mode" = detect ] || fail "unknown option: $option"; fail_on=$1 ;;
                esac ;;
            --output=*|-o?*)
                [ "$mode" != review-merge ] || fail "unknown option: $1"
                case "$1" in --output=*) output=${1#--output=} ;; *) output=${1#-o} ;; esac
                [ -n "$output" ] || fail 'missing value for --output' ;;
            --output-dir=*) [ "$mode" != review-merge ] || fail "unknown option: $1"; output_dir=${1#--output-dir=}; [ -n "$output_dir" ] || fail 'missing value for --output-dir' ;;
            --report=*) report=${1#--report=}; [ -n "$report" ] || fail 'missing value for --report' ;;
            --review-dir=*) [ "$mode" != clean ] || fail "unknown option: $1"; review=${1#--review-dir=}; [ -n "$review" ] || fail 'missing value for --review-dir' ;;
            --fail-on=*) [ "$mode" = detect ] || fail "unknown option: $1"; fail_on=${1#--fail-on=}; [ -n "$fail_on" ] || fail 'missing value for --fail-on' ;;
            --json) [ "$mode" = clean ] || [ "$mode" = detect ] || fail "unknown option: $1"; json=1 ;;
            --agent-summary) agent=1 ;;
            --sanitize) [ "$mode" = detect ] || fail "unknown option: $1"; sanitized=1 ;;
            --force-output) [ "$mode" != review-merge ] || fail "unknown option: $1"; force=1 ;;
            --prompt-guard) [ "$mode" = detect ] || fail "unknown option: $1"; prompt_guard=1 ;;
            --)
                shift
                [ "$#" -eq 1 ] && [ -z "$input" ] || fail 'expected exactly one input file'
                input=$1; shift; break ;;
            -*) fail "unknown option: $1" ;;
            *) [ "$mode" != review-merge ] && [ -z "$input" ] || fail 'expected exactly one input file'; input=$1 ;;
        esac
        shift
    done
    [ "$json" -eq 0 ] || [ "$agent" -eq 0 ] || fail '--json and --agent-summary are mutually exclusive'
    [ -z "$output" ] || [ -z "$output_dir" ] || fail '--output and --output-dir are mutually exclusive'
    if [ "$mode" = detect ] && [ "$sanitized" -eq 0 ] && { [ -n "$output" ] || [ -n "$output_dir" ] || [ "$force" -eq 1 ]; }; then
        fail '--output and --force-output require --sanitize'
    fi
    if [ "$mode" = sanitize ] && [ -z "$review" ]; then fail 'sanitize requires --review-dir'; fi
    case "$fail_on" in ''|high|medium) ;; *) fail 'invalid --fail-on value (choose high or medium)' ;; esac
    if [ "$mode" != review-merge ]; then
        [ -n "$input" ] || fail 'expected an input file'
        [ -f "$input" ] || {
            case "$input" in /*) missing=$input ;; *) missing=$PWD/$input ;; esac
            fail "input not found: $missing"
        }
        input_given=$input
        input=$(resolve_path "$input") || fail 'cannot resolve input path'
        in_dir=$(dirname "$input"); in_name=$(basename "$input")
    else
        [ -n "$review" ] || fail 'review-merge requires --review-dir'
    fi
    if [ -n "$review" ]; then
        review=$(resolve_path "$review") || fail 'review parent directory does not exist'
        [ "$review" != / ] || fail 'review directory must not contain the input document'
        if [ -n "$input" ]; then
            case "$input" in "$review"/*) fail 'review directory must not contain the input document' ;; esac
        fi
        if [ "$mode" = detect ]; then
            (umask 077; mkdir -p "$review") || fail 'cannot create review directory'
        else
            [ -d "$review" ] || fail 'review directory does not exist'
        fi
    fi
    if [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; then
        if [ "$mode" = clean ] && [ -z "$output" ] && [ -z "$output_dir" ]; then
            default_clean=1
            case "$in_name" in
                .*.*|?*.*) case "$in_name" in *.) out_name="$in_name.cleaned" ;; *) out_name="${in_name%.*}.cleaned.${in_name##*.}" ;; esac ;;
                *) out_name="$in_name.cleaned" ;;
            esac
            default_clean_path=$in_dir/$out_name
            [ ! -L "$default_clean_path" ] || output=$default_clean_path
        fi
        if [ -n "$output" ]; then
            output=$(resolve_path "$output") || fail 'output directory does not exist'
            same_path "$input" "$output" && fail 'output path must differ from the input (no in-place cleaning)'
            out_dir=$(dirname "$output"); out_name=$(basename "$output")
        else
            [ -n "$output_dir" ] || output_dir=$in_dir
            [ -d "$output_dir" ] || fail 'output directory does not exist'
            out_dir=$(realpath "$output_dir") || fail 'output directory does not exist'
        fi
    fi
    if [ -n "$report" ]; then
        report=$(resolve_path "$report") || fail 'report directory does not exist'
        if [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; then
            if same_path "$report" "$input" || { [ -n "$output" ] && same_path "$report" "$output"; }; then
                fail 'report path must differ from the input and the output document'
            fi
        elif [ -n "$input" ] && same_path "$report" "$input"; then
            fail 'report path must differ from the input document'
        fi
        report_dir=$(dirname "$report"); report_name=$(basename "$report")
    fi
fi

command -v docker >/dev/null 2>&1 || fail 'docker is not installed or not on PATH'
if ! docker info >/dev/null 2>&1; then
    if [ "$(uname -s)" = Darwin ]; then
        fail 'Docker daemon is not available; open Docker Desktop and try again'
    fi
    fail 'Docker daemon is not available'
fi

script_dir=$(CDPATH='' cd -P -- "$(dirname "$0")" && pwd -P) || fail 'cannot locate launcher'
repo=$(dirname "$script_dir")
# Record which commit built the image in a local stamp (some engines drop image labels),
# so a stale image is rebuilt after a pull. Uncommitted changes always rebuild and leave
# no stamp. Without git, an existing image is reused. Refreshing a stale image is best
# effort: if it cannot be built (e.g. a worktree without submodules), the existing image
# is used with a warning. A missing image or --rebuild must build.
required=$rebuild
source_label=''; stamp="$repo/.cache/doc-sentinel-image"
if command -v git >/dev/null 2>&1 && [ "$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null)" = "$repo" ] &&
    head=$(git -C "$repo" rev-parse HEAD 2>/dev/null) && [ -n "$head" ]; then
    # Build inputs only (see containers/Dockerfile), including untracked files.
    if changes=$(git -C "$repo" status --porcelain -- src third_party containers pyproject.toml \
        README.md LICENSE pins.lock.json 2>/dev/null) && [ -z "$changes" ]; then
        source_label=$head
    else
        rebuild=1
    fi
fi
if image_id=$(docker image inspect --format '{{.Id}}' doc-sentinel:local 2>/dev/null); then
    if [ -n "$source_label" ]; then
        recorded=''
        if [ -f "$stamp" ]; then read -r recorded < "$stamp" || :; fi
        [ "$recorded" = "$source_label $image_id" ] || rebuild=1
    fi
else
    rebuild=1; required=1
fi
stale_warning='doc-sentinel: could not refresh the outdated image; using the existing one'
if [ "$rebuild" -eq 1 ]; then
    [ -f "$repo/containers/Dockerfile" ] || fail "Dockerfile not found: $repo/containers/Dockerfile"
    found=0; populated_all=1
    for dir in "$repo"/third_party/*; do
        [ -d "$dir" ] || continue
        found=1; populated=0
        for entry in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
            [ "$(basename "$entry")" = .git ] && continue
            if [ -e "$entry" ] || [ -L "$entry" ]; then populated=1; break; fi
        done
        [ "$populated" -eq 1 ] || populated_all=0
    done
    [ "$found" -eq 1 ] || populated_all=0
    if [ "$populated_all" -eq 0 ]; then
        [ "$required" -eq 0 ] || fail 'run: git submodule update --init --recursive'
        printf '%s (run: git submodule update --init --recursive)\n' "$stale_warning" >&2
        rebuild=0
    fi
fi
if [ "$rebuild" -eq 1 ]; then
    if [ -f "$stamp" ]; then { true > "$stamp"; } 2>/dev/null || :; fi
    # Retry once: some BuildKit setups intermittently fail to resolve base images.
    docker build -f "$repo/containers/Dockerfile" -t doc-sentinel:local "$repo" 1>&2
    build_code=$?
    if [ "$build_code" -ne 0 ]; then
        docker build -f "$repo/containers/Dockerfile" -t doc-sentinel:local "$repo" 1>&2
        build_code=$?
    fi
    if [ "$build_code" -ne 0 ]; then
        [ "$required" -eq 0 ] || exit "$build_code"
        printf '%s (build failed)\n' "$stale_warning" >&2
    elif [ -n "$source_label" ] && image_id=$(docker image inspect --format '{{.Id}}' doc-sentinel:local 2>/dev/null); then
        { mkdir -p "$repo/.cache" && printf '%s %s\n' "$source_label" "$image_id" > "$stamp"; } 2>/dev/null || :
    fi
fi

# Save intent/version arguments as positional parameters, then prepend Docker arguments.
if [ "$mode" = clean ] || [ "$mode" = detect ] || [ "$mode" = sanitize ] || [ "$mode" = review-merge ]; then
    if [ "$mode" = review-merge ]; then set -- "$mode"; else set -- "$mode" "/work/in/$in_name"; fi
    if [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; then
        if [ -n "$output" ]; then
            set -- "$@" -o "/work/out/$out_name"
        else
            set -- "$@" --output-dir /work/out
        fi
        [ "$force" -eq 0 ] || set -- "$@" --force-output
    fi
    [ "$json" -eq 0 ] || set -- "$@" --json
    [ "$agent" -eq 0 ] || set -- "$@" --agent-summary
    if [ -n "$report" ]; then
        if { [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; } && [ "$report_dir" = "$out_dir" ]; then
            container_report=/work/out/$report_name
        else
            container_report=/work/report/$report_name
        fi
        set -- "$@" --report "$container_report"
    fi
    [ -z "$fail_on" ] || set -- "$@" --fail-on "$fail_on"
    [ "$prompt_guard" -eq 0 ] || set -- "$@" --prompt-guard
    [ -z "$review" ] || set -- "$@" --review-dir /work/review
    if [ "$mode" = detect ] && [ "$sanitized" -eq 1 ]; then set -- "$@" --sanitize; fi
else
    set -- "$mode" "$@"
fi
set -- doc-sentinel:local "$@"
if [ "$mode" = clean ] || [ "$mode" = detect ] || [ "$mode" = sanitize ] || [ "$mode" = review-merge ]; then
    [ -z "$review" ] || set -- -v "$review:/work/review:rw" "$@"
    if [ -n "$report" ] && { { [ "$mode" != clean ] && [ "$sanitized" -eq 0 ]; } || [ "$report_dir" != "$out_dir" ]; }; then
        set -- -v "$report_dir:/work/report:rw" "$@"
    fi
    if [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; then set -- -v "$out_dir:/work/out:rw" "$@"; fi
    if [ "$mode" != review-merge ]; then set -- -v "$in_dir:/work/in:ro" "$@"; fi
    if [ "$agent" -eq 1 ]; then
        [ -z "$review" ] || set -- -e "DOC_SENTINEL_HOST_REVIEW=$review" "$@"
        [ -z "$report" ] || set -- -e "DOC_SENTINEL_HOST_REPORT=$report" "$@"
        [ -z "$output" ] || set -- -e "DOC_SENTINEL_HOST_OUTPUT=$output" "$@"
        if { [ "$mode" = clean ] || [ "$sanitized" -eq 1 ]; } && [ -z "$output" ]; then
            set -- -e "DOC_SENTINEL_HOST_OUTPUT_DIR=$out_dir" "$@"
        fi
        [ -z "$input" ] || set -- -e "DOC_SENTINEL_HOST_INPUT=$input_given" "$@"
    fi
fi
if [ "$(uname -s)" = Linux ]; then set -- --user "$(id -u):$(id -g)" "$@"; fi
set -- run --rm --network=none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    --cap-drop=ALL --security-opt=no-new-privileges -e DOC_SENTINEL_NO_NETNS=1 "$@"
docker "$@"
code=$?
if [ "$mode" = clean ] && [ "$code" -eq 0 ]; then
    if [ -n "$output" ]; then
        printf 'Cleaned copy: %s\n' "$output" >&2
    elif [ "$default_clean" -eq 1 ]; then
        clean_path=$(resolve_path "$default_clean_path") || clean_path=$default_clean_path
        printf 'Cleaned copy: %s\n' "$clean_path" >&2
    fi
fi
exit "$code"
