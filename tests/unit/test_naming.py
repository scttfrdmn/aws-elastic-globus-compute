import pytest

from aegc import naming


@pytest.mark.parametrize("good", ["dev", "a", "sci-py314", "a1-b2", "x" * 20])
def test_valid_names(good: str) -> None:
    assert naming.validate_name("deployment", good) == good


@pytest.mark.parametrize("bad", ["", "Dev", "1dev", "dev-", "a--b", "x" * 21, "dev_1", "dev.1"])
def test_invalid_names(bad: str) -> None:
    with pytest.raises(naming.NameError_):
        naming.validate_name("deployment", bad)


def test_tags_and_reserved_prefix() -> None:
    t = naming.tags("dev", "task-queue", pool="sci", extra={"cost-center": "42"})
    assert t[naming.TAG_MANAGED] == "true"
    assert t[naming.TAG_DEPLOYMENT] == "dev"
    assert t[naming.TAG_POOL] == "sci"
    assert t["cost-center"] == "42"
    with pytest.raises(ValueError, match="reserved"):
        naming.tags("dev", "x", extra={"aegc:managed": "false"})


def test_tag_conversions() -> None:
    t = {"a": "1", "b": "2"}
    assert naming.tag_dict(naming.tag_list(t)) == t
    assert naming.tag_dict(t) == t
    assert naming.tag_dict(None) == {}


OURS = naming.tags("dev", "task-queue")


@pytest.mark.parametrize(
    ("name", "tags", "deployment", "owned"),
    [
        ("aegc-dev-sci-tasks", OURS, "dev", True),
        ("aegc-dev", OURS, "dev", True),
        ("arn:aws:iam::1:role/aegc/aegc-dev-worker-us-west-2", OURS, "dev", True),
        # Name matches but tags don't: someone else's look-alike.
        ("aegc-dev-sci-tasks", {}, "dev", False),
        ("aegc-dev-sci-tasks", {**OURS, naming.TAG_MANAGED: "false"}, "dev", False),
        # Tags match but the name isn't ours.
        ("other-queue", OURS, "dev", False),
        # Another deployment's resource, even with a prefix that looks similar.
        ("aegc-dev2-sci-tasks", naming.tags("dev2", "task-queue"), "dev", False),
        ("aegc-devx-tasks", OURS, "dev", False),
        # Bootstrap resources.
        ("aegc-1-us-west-2", naming.tags(naming.BOOTSTRAP, "bucket"), naming.BOOTSTRAP, True),
        ("aegc-1-us-west-2", naming.tags(naming.BOOTSTRAP, "bucket"), "dev", False),
    ],
)
def test_is_owned(name: str, tags: dict[str, str], deployment: str, owned: bool) -> None:
    assert naming.is_owned(name, tags, deployment) is owned


def test_names() -> None:
    eid = "12345678-90ab-cdef-1234-567890abcdef"
    assert naming.result_queue("dev", eid) == "aegc-dev-res-1234567890ab"
    assert naming.task_queue("dev", "sci-py314") == "aegc-dev-sci-py314-tasks"
    assert naming.worker_role("dev", "us-west-2") == "aegc-dev-worker-us-west-2"
    assert naming.deployment_manifest_parameter("dev") == "/aegc/dev/manifest"
    # SQS queue names are limited to 80 characters.
    assert len(naming.dead_letter_queue("x" * 20, "y" * 20)) <= 80
