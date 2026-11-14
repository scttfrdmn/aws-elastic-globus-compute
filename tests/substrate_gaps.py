"""Stand-ins for the few AWS operations substrate doesn't emulate yet.

Each shim names the substrate issue that tracks the missing capability. When substrate gains it,
delete the shim and its use; the test then exercises substrate directly.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class Shim:
    """Wrap a real boto3 client, replacing selected methods with in-memory fakes."""

    def __init__(self, client: Any, **overrides: Any) -> None:
        self._client = client
        self._overrides = overrides
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            fake = self._overrides[name]

            def call(**kw: Any) -> Any:
                self.calls.append((name, kw))
                return fake(**kw)

            return call
        return getattr(self._client, name)


def s3_with_ownership_controls(s3: Any) -> Shim:
    """substrate#1445: PutBucketOwnershipControls returns 501."""
    return Shim(s3, put_bucket_ownership_controls=lambda **kw: {})


class FakeEcrPullThrough:
    """substrate#1461: pull-through cache rules and repository creation templates."""

    def __init__(self) -> None:
        self.rules: dict[str, str] = {}
        self.templates: dict[str, dict[str, Any]] = {}

    def wrap(self, ecr: Any) -> Shim:
        return Shim(
            ecr,
            create_pull_through_cache_rule=self.create_rule,
            describe_pull_through_cache_rules=self.describe_rules,
            delete_pull_through_cache_rule=self.delete_rule,
            create_repository_creation_template=self.create_template,
            describe_repository_creation_templates=self.describe_templates,
            delete_repository_creation_template=self.delete_template,
            update_repository_creation_template=self.update_template,
        )

    def create_rule(self, *, ecrRepositoryPrefix: str, upstreamRegistryUrl: str) -> dict[str, Any]:
        if ecrRepositoryPrefix in self.rules:
            raise _client_error(
                "PullThroughCacheRuleAlreadyExistsException", "CreatePullThroughCacheRule"
            )
        self.rules[ecrRepositoryPrefix] = upstreamRegistryUrl
        return {"ecrRepositoryPrefix": ecrRepositoryPrefix}

    def describe_rules(self, *, ecrRepositoryPrefixes: list[str]) -> dict[str, Any]:
        found = [
            {"ecrRepositoryPrefix": p, "upstreamRegistryUrl": self.rules[p]}
            for p in ecrRepositoryPrefixes
            if p in self.rules
        ]
        if not found:
            raise _client_error(
                "PullThroughCacheRuleNotFoundException", "DescribePullThroughCacheRules"
            )
        return {"pullThroughCacheRules": found}

    def delete_rule(self, *, ecrRepositoryPrefix: str) -> dict[str, Any]:
        if self.rules.pop(ecrRepositoryPrefix, None) is None:
            raise _client_error(
                "PullThroughCacheRuleNotFoundException", "DeletePullThroughCacheRule"
            )
        return {}

    def create_template(self, *, prefix: str, **kw: Any) -> dict[str, Any]:
        if prefix in self.templates:
            raise _client_error(
                "TemplateAlreadyExistsException", "CreateRepositoryCreationTemplate"
            )
        self.templates[prefix] = kw
        return {"repositoryCreationTemplate": {"prefix": prefix, **kw}}

    def update_template(self, *, prefix: str, **kw: Any) -> dict[str, Any]:
        if prefix not in self.templates:
            raise _client_error("TemplateNotFoundException", "UpdateRepositoryCreationTemplate")
        self.templates[prefix].update(kw)
        return {"repositoryCreationTemplate": {"prefix": prefix, **self.templates[prefix]}}

    def describe_templates(self, *, prefixes: list[str]) -> dict[str, Any]:
        found = [{"prefix": p, **self.templates[p]} for p in prefixes if p in self.templates]
        if not found:
            raise _client_error("TemplateNotFoundException", "DescribeRepositoryCreationTemplates")
        return {"repositoryCreationTemplates": found}

    def delete_template(self, *, prefix: str) -> dict[str, Any]:
        if self.templates.pop(prefix, None) is None:
            raise _client_error("TemplateNotFoundException", "DeleteRepositoryCreationTemplate")
        return {}
