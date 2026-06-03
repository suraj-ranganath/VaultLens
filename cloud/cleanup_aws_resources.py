#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


VAULT_REPO_PATTERN = re.compile(r"^(vault-lens|my-vault).*?(lambda|receiver|processor)")
VAULT_FUNCTION_PATTERN = re.compile(r"^(vault-lens|my-vault).*")
SHA_RE = re.compile(r"@(?P<digest>sha256:[0-9a-f]{64})$")


def aws(args: list[str], *, region: str, output_json: bool = True) -> Any:
    cmd = [
        "aws",
        *args,
        "--region",
        region,
        "--cli-connect-timeout",
        "5",
        "--cli-read-timeout",
        "20",
        "--no-cli-pager",
    ]
    if output_json:
        cmd.extend(["--output", "json"])
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{proc.stderr.strip()}")
    if output_json:
        return json.loads(proc.stdout or "null")
    return proc.stdout


def lifecycle_policy(
    *,
    untagged_days: int,
    keep_app_images: int,
    keep_browser_images: int,
    keep_total_images: int,
) -> dict[str, Any]:
    return {
        "rules": [
            {
                "rulePriority": 1,
                "description": "Expire untagged deployment layers quickly",
                "selection": {
                    "tagStatus": "untagged",
                    "countType": "sinceImagePushed",
                    "countUnit": "days",
                    "countNumber": untagged_days,
                },
                "action": {"type": "expire"},
            },
            {
                "rulePriority": 10,
                "description": "Keep only the latest browser-worker images",
                "selection": {
                    "tagStatus": "tagged",
                    "tagPrefixList": ["browserworkerfunction-"],
                    "countType": "imageCountMoreThan",
                    "countNumber": keep_browser_images,
                },
                "action": {"type": "expire"},
            },
            {
                "rulePriority": 20,
                "description": "Keep only the latest app Lambda images",
                "selection": {
                    "tagStatus": "tagged",
                    "tagPrefixList": ["processorfunction-"],
                    "countType": "imageCountMoreThan",
                    "countNumber": keep_app_images,
                },
                "action": {"type": "expire"},
            },
            {
                "rulePriority": 100,
                "description": "Hard cap retained deployment images",
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": keep_total_images,
                },
                "action": {"type": "expire"},
            },
        ]
    }


def list_repositories(region: str) -> list[str]:
    payload = aws(["ecr", "describe-repositories"], region=region)
    names = [repo["repositoryName"] for repo in payload.get("repositories", [])]
    return sorted(name for name in names if VAULT_REPO_PATTERN.search(name))


def list_vault_functions(region: str) -> list[str]:
    functions: list[str] = []
    marker = None
    while True:
        args = ["lambda", "list-functions"]
        if marker:
            args.extend(["--marker", marker])
        payload = aws(args, region=region)
        functions.extend(
            fn["FunctionName"]
            for fn in payload.get("Functions", [])
            if VAULT_FUNCTION_PATTERN.search(fn.get("FunctionName", ""))
        )
        marker = payload.get("NextMarker")
        if not marker:
            break
    return sorted(functions)


def active_image_digests_by_repo(region: str, repositories: set[str]) -> dict[str, set[str]]:
    protected: dict[str, set[str]] = {repo: set() for repo in repositories}
    for function_name in list_vault_functions(region):
        payload = aws(["lambda", "get-function", "--function-name", function_name], region=region)
        resolved = payload.get("Code", {}).get("ResolvedImageUri") or ""
        if not resolved:
            continue
        repo_uri, _, digest_part = resolved.partition("@")
        repo = repo_uri.rsplit("/", 1)[-1]
        digest = f"@{digest_part}" if digest_part else ""
        match = SHA_RE.search(digest)
        if repo in protected and match:
            protected[repo].add(match.group("digest"))
    return protected


def parse_pushed_at(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def image_kind(tags: list[str]) -> str:
    if any(tag.startswith("browserworkerfunction-") for tag in tags):
        return "browser"
    if any(tag.startswith("processorfunction-") or tag.startswith("receiverfunction-") for tag in tags):
        return "app"
    return "other"


def list_images(region: str, repository: str) -> list[dict[str, Any]]:
    payload = aws(["ecr", "describe-images", "--repository-name", repository], region=region)
    images = payload.get("imageDetails", [])
    return sorted(images, key=lambda img: parse_pushed_at(img.get("imagePushedAt")), reverse=True)


def select_images_to_delete(
    images: list[dict[str, Any]],
    *,
    protected_digests: set[str],
    keep_app_images: int,
    keep_browser_images: int,
    keep_total_images: int,
) -> list[dict[str, Any]]:
    keep_by_kind = {"app": keep_app_images, "browser": keep_browser_images, "other": 1}
    kind_counts = {"app": 0, "browser": 0, "other": 0}
    delete_digests: set[str] = set()

    for image in images:
        digest = image["imageDigest"]
        if digest in protected_digests:
            continue
        tags = image.get("imageTags") or []
        kind = image_kind(tags)
        kind_counts[kind] += 1
        if kind_counts[kind] > keep_by_kind[kind]:
            delete_digests.add(digest)

    remaining = [
        image
        for image in images
        if image["imageDigest"] not in delete_digests
    ]
    retained_count = len(remaining)
    for image in reversed(remaining):
        digest = image["imageDigest"]
        if retained_count <= keep_total_images:
            break
        if digest in protected_digests:
            continue
        delete_digests.add(digest)
        retained_count -= 1

    return [image for image in images if image["imageDigest"] in delete_digests]


def put_lifecycle_policy(region: str, repository: str, policy: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    aws(
        [
            "ecr",
            "put-lifecycle-policy",
            "--repository-name",
            repository,
            "--lifecycle-policy-text",
            json.dumps(policy),
        ],
        region=region,
    )


def delete_images(region: str, repository: str, images: list[dict[str, Any]], *, dry_run: bool) -> None:
    if dry_run or not images:
        return
    for start in range(0, len(images), 100):
        chunk = images[start : start + 100]
        aws(
            [
                "ecr",
                "batch-delete-image",
                "--repository-name",
                repository,
                "--image-ids",
                json.dumps([{"imageDigest": image["imageDigest"]} for image in chunk]),
            ],
            region=region,
        )


def delete_repository(region: str, repository: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    aws(
        ["ecr", "delete-repository", "--repository-name", repository, "--force"],
        region=region,
    )


def list_legacy_schedules(region: str, legacy_prefixes: list[str]) -> list[dict[str, Any]]:
    schedules: list[dict[str, Any]] = []
    token = None
    while True:
        args = ["scheduler", "list-schedules"]
        if token:
            args.extend(["--next-token", token])
        payload = aws(args, region=region)
        for schedule in payload.get("Schedules", []):
            name = schedule.get("Name", "")
            if any(name.startswith(prefix) for prefix in legacy_prefixes):
                schedules.append(schedule)
        token = payload.get("NextToken")
        if not token:
            break
    return schedules


def disable_schedule(region: str, schedule_name: str, *, dry_run: bool) -> bool:
    schedule = aws(["scheduler", "get-schedule", "--name", schedule_name], region=region)
    if schedule.get("State") == "DISABLED":
        return False

    target = {
        key: value
        for key, value in schedule.get("Target", {}).items()
        if key in {"Arn", "RoleArn", "Input", "DeadLetterConfig", "RetryPolicy", "KmsKeyArn"}
    }
    args = [
        "scheduler",
        "update-schedule",
        "--name",
        schedule_name,
        "--state",
        "DISABLED",
        "--schedule-expression",
        schedule["ScheduleExpression"],
        "--flexible-time-window",
        json.dumps(schedule["FlexibleTimeWindow"]),
        "--target",
        json.dumps(target),
    ]
    timezone_name = schedule.get("ScheduleExpressionTimezone")
    if timezone_name:
        args.extend(["--schedule-expression-timezone", timezone_name])
    group_name = schedule.get("GroupName")
    if group_name and group_name != "default":
        args.extend(["--group-name", group_name])

    if not dry_run:
        aws(args, region=region)
    return True


def image_size_gb(images: list[dict[str, Any]]) -> float:
    return sum(int(image.get("imageSizeInBytes") or 0) for image in images) / 1_000_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean VaultLens AWS deploy artifacts safely.")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without mutating AWS.")
    parser.add_argument("--no-prune-images", action="store_true", help="Only install ECR lifecycle policies.")
    parser.add_argument("--no-disable-legacy-schedules", action="store_true")
    parser.add_argument(
        "--delete-unused-legacy-repos",
        action="store_true",
        help="Delete legacy ECR repos that have no image digest referenced by any Lambda function.",
    )
    parser.add_argument("--legacy-prefix", action="append", default=["my-vault-telegram-"])
    parser.add_argument("--untagged-days", type=int, default=1)
    parser.add_argument("--keep-app-images", type=int, default=4)
    parser.add_argument("--keep-browser-images", type=int, default=2)
    parser.add_argument("--keep-total-images", type=int, default=6)
    parser.add_argument("--legacy-keep-total-images", type=int, default=2)
    args = parser.parse_args()

    repositories = list_repositories(args.region)
    protected = active_image_digests_by_repo(args.region, set(repositories))
    policy = lifecycle_policy(
        untagged_days=max(1, args.untagged_days),
        keep_app_images=max(1, args.keep_app_images),
        keep_browser_images=max(1, args.keep_browser_images),
        keep_total_images=max(1, args.keep_total_images),
    )

    report: dict[str, Any] = {
        "dry_run": args.dry_run,
        "region": args.region,
        "repositories": [],
        "disabled_schedules": [],
    }

    for repository in repositories:
        images = list_images(args.region, repository)
        is_legacy = any(repository.startswith(prefix.rstrip("-")) for prefix in args.legacy_prefix)
        delete_repository_after = args.delete_unused_legacy_repos and is_legacy and not protected.get(repository)
        keep_total = args.legacy_keep_total_images if is_legacy else args.keep_total_images
        if args.no_prune_images:
            to_delete = []
        elif delete_repository_after:
            to_delete = images
        else:
            to_delete = select_images_to_delete(
                images,
                protected_digests=protected.get(repository, set()),
                keep_app_images=args.keep_app_images,
                keep_browser_images=args.keep_browser_images,
                keep_total_images=keep_total,
            )
        if not delete_repository_after:
            put_lifecycle_policy(args.region, repository, policy, dry_run=args.dry_run)
        delete_images(args.region, repository, to_delete, dry_run=args.dry_run)
        if delete_repository_after:
            delete_repository(args.region, repository, dry_run=args.dry_run)
        report["repositories"].append(
            {
                "name": repository,
                "image_count_before": len(images),
                "reported_gb_before": round(image_size_gb(images), 3),
                "protected_digests": sorted(protected.get(repository, set())),
                "lifecycle_policy_installed": (not args.dry_run) and (not delete_repository_after),
                "delete_repository": delete_repository_after,
                "repository_deleted": delete_repository_after and not args.dry_run,
                "delete_count": len(to_delete),
                "delete_reported_gb": round(image_size_gb(to_delete), 3),
            }
        )

    if not args.no_disable_legacy_schedules:
        for schedule in list_legacy_schedules(args.region, args.legacy_prefix):
            changed = disable_schedule(args.region, schedule["Name"], dry_run=args.dry_run)
            report["disabled_schedules"].append(
                {
                    "name": schedule["Name"],
                    "previous_state": schedule.get("State"),
                    "changed": changed,
                }
            )

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
