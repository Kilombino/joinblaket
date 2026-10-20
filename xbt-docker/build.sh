#!/bin/sh
# Build the JoinMarket-NG XBT + Jam image from committed sources only.
#   xbt-docker/build.sh [JAM_REPO_DIR] [TAG]
# The Jam fork is taken from git (branch/commit given by JAM_REF, default "xbt").
#
# Reproducible: SOURCE_DATE_EPOCH = time of the joinmarket-ng commit, file
# times clamped with rewrite-timestamp, no provenance/SBOM attestations
# (they carry build times). Same commits => same image digest.
#
# Environment:
#   PLATFORM  linux/amd64 (default), linux/arm64, or both comma-separated
#   BUILDER   buildx builder (docker-container driver). Default: the current
#             one if it is docker-container, else one named "jam-xbt" is created.
#   OCI       write an OCI archive to this path instead of loading into Docker
#             (needed for several platforms at once; prints the digest)
#   PUSH      1 = push to the registry (the tag's) instead of loading or OCI
#   NO_CACHE  1 = --no-cache
set -eu
REPO=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
JAM_DIR=${1:-../jam-xbt}
TAG=${2:-ghcr.io/kilombino/jam-xbt:0.41.0-xbt1}
JAM_REF=${JAM_REF:-xbt}
PLATFORM=${PLATFORM:-linux/amd64}
BUILDER=${BUILDER:-}
OCI=${OCI:-}

if [ -z "$BUILDER" ]; then
    if docker buildx inspect 2>/dev/null | grep -q '^Driver:[[:space:]]*docker-container'; then
        BUILDER=$(docker buildx inspect | awk '/^Name:/ {print $2; exit}')
    else
        BUILDER=jam-xbt
        docker buildx inspect "$BUILDER" >/dev/null 2>&1 ||
            docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
    fi
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/jm" "$work/jam"
git -C "$REPO" archive HEAD | tar -x -C "$work/jm"
git -C "$JAM_DIR" archive "$JAM_REF" | tar -x -C "$work/jam"

JM_COMMIT=$(git -C "$REPO" rev-parse HEAD)
JAM_COMMIT=$(git -C "$JAM_DIR" rev-parse "$JAM_REF")
EPOCH=$(git -C "$REPO" log -1 --format=%ct)
echo "joinmarket-ng $JM_COMMIT, jam $JAM_COMMIT, platform $PLATFORM, SOURCE_DATE_EPOCH $EPOCH, builder $BUILDER"

if [ -n "${PUSH:-}" ]; then
    output="type=image,name=$TAG,push=true,rewrite-timestamp=true"
elif [ -n "$OCI" ]; then
    output="type=oci,dest=$OCI,name=$TAG,rewrite-timestamp=true"
else
    output="type=docker,name=$TAG,rewrite-timestamp=true"
fi

SOURCE_DATE_EPOCH=$EPOCH docker buildx build --builder "$BUILDER" \
    --platform "$PLATFORM" \
    ${NO_CACHE:+--no-cache} \
    --provenance=false --sbom=false \
    --build-context jam="$work/jam" \
    --build-arg SOURCE_DATE_EPOCH="$EPOCH" \
    --build-arg JOINMARKET_BUILD_COMMIT="$JM_COMMIT" \
    --build-arg JOINMARKET_BUILD_REF=xbt \
    --label org.opencontainers.image.revision="$JM_COMMIT" \
    --label org.opencontainers.image.version=0.41.0-xbt1 \
    --label org.kilombino.jam.revision="$JAM_COMMIT" \
    --label org.opencontainers.image.source=https://github.com/Kilombino/joinmarket-ng \
    --output "$output" \
    -f "$work/jm/xbt-docker/Dockerfile" "$work/jm"

if [ -n "${PUSH:-}" ]; then
    docker buildx imagetools inspect "$TAG" | sed -n '1,3p'
elif [ -n "$OCI" ]; then
    # The digest a registry will show for this tag (index if several platforms).
    echo "image digest: $(tar -xOf "$OCI" index.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["manifests"][0]["digest"])')"
else
    echo "image id (config digest): $(docker image inspect --format '{{.Id}}' "$TAG")"
fi
