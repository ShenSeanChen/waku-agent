"""Idle stop, starting, paused and the running cap -- acceptance 10.

A fake clock and a fake fleet. No Docker: the spawner's side of this arrives
in group C and the gateway's HTTP side in group E; these are the decisions
both of them will make.

NOTHING BELOW CALLS touch() TO SET UP A CASE IT IS ABOUT TO ASSERT ON. The
first draft of this file did, in the one test that mattered, and it passed
whatever set_status did -- a test that cannot fail for the reason it exists.
Where a test is about whether the idle clock was refreshed, the refresh has to
come from the code under test.
"""

from __future__ import annotations

from hosted.core import idle, policy


def fleet(max_running: int = 2):
    clock = {"t": 1_000_000.0}
    return clock, idle.Fleet(lambda: clock["t"], max_running=max_running)


def test_a_background_request_never_starts_a_container():
    """The dashboard polls every few seconds forever. If a poll could start a
    container, nothing would ever stay stopped."""
    _clock, f = fleet()
    admission = f.admit("abcdefghijkl", background=True)
    assert admission.action == "paused"
    assert admission.message == policy.PAUSED_BODY["error"]
    assert f.running() == []


def test_a_user_request_starts_one():
    _clock, f = fleet()
    assert f.admit("abcdefghijkl", background=False).action == "start"


def test_a_request_to_a_starting_container_waits_rather_than_reading_paused():
    """Otherwise the first page load after a start shows the paused banner
    while its own container is coming up."""
    _clock, f = fleet()
    f.set_status("abcdefghijkl", idle.STARTING)
    assert f.admit("abcdefghijkl", background=False).action == "wait"
    assert f.admit("abcdefghijkl", background=True).action == "wait"


def test_a_running_container_is_forwarded_to():
    _clock, f = fleet()
    f.adopt("abcdefghijkl")
    assert f.admit("abcdefghijkl", background=True).action == "forward"


def test_a_restart_after_an_idle_stop_survives_the_next_sweep():
    """THE CYCLE THAT MATTERS. A tenant comes back an hour after their
    container was stopped for being idle. The start is caused by their
    request, so the start IS activity; if nothing records that, the idle
    sweep sixty seconds later sees a last_activity from an hour ago and stops
    the container the user just waited through a cold start for. They then
    poll, the poll is background, and they get `paused` instead of a restart.

    Nothing here calls touch() after the stop. That is the point: the only
    thing that can refresh the clock on this path is admit().
    """
    clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    f.touch("abcdefghijkl")                      # one real request, before the stop

    clock["t"] += idle.IDLE_SECONDS + 1
    assert f.idle_stops() == ["abcdefghijkl"]
    f.set_status("abcdefghijkl", idle.STOPPED)   # the gateway stops it

    clock["t"] += 3600                           # the tenant is away for an hour
    assert f.admit("abcdefghijkl", background=False).action == "start"
    f.set_status("abcdefghijkl", idle.STARTING)
    f.set_status("abcdefghijkl", idle.RUNNING)

    clock["t"] += 60                             # the next idle sweep
    assert f.idle_stops() == [], (
        "the container was stopped sixty seconds after the user started it")


def test_an_eviction_start_is_activity_too():
    """The same hole, on the other start path."""
    clock, f = fleet(max_running=1)
    f.set_status("aaaaaaaaaaaa", idle.RUNNING)
    f.touch("aaaaaaaaaaaa")
    clock["t"] += idle.IDLE_SECONDS + 1

    admission = f.admit("bbbbbbbbbbbb", background=False)
    assert admission.action == "evict_then_start" and admission.evict == "aaaaaaaaaaaa"
    f.set_status("aaaaaaaaaaaa", idle.STOPPED)
    f.set_status("bbbbbbbbbbbb", idle.RUNNING)

    clock["t"] += 60
    assert f.idle_stops() == [], "the evicting tenant's own container was stopped next"


def test_adopting_a_running_container_starts_its_idle_timer_fresh():
    """When the gateway starts it asks the spawner to list running containers
    and treats each as active from that moment, so the idle timer starts fresh
    and no container is orphaned.

    Nothing here calls touch(): if adopt() does not set the clock,
    last_activity stays 0.0, every adopted container is instantly older than
    the idle window, and the first sweep after a gateway restart stops every
    container on the VM -- including the ones people are using.
    """
    clock, f = fleet()
    f.adopt("abcdefghijkl")
    clock["t"] += idle.IDLE_SECONDS - 1
    assert f.idle_stops() == [], "an adopted container was stopped inside the idle window"
    clock["t"] += 2
    assert f.idle_stops() == ["abcdefghijkl"], "an adopted container never idles out"


def test_adopting_again_never_clobbers_a_clock_the_fleet_already_has():
    """A re-list mid-life -- the spec's "looked up again with the spawner's
    list" after a refused connection -- must not reset the idle clock. Without
    the "only when we have none" condition, a container that refuses
    connections would be re-listed on every request and never idle out."""
    clock, f = fleet()
    f.adopt("abcdefghijkl")
    clock["t"] += idle.IDLE_SECONDS - 10
    f.adopt("abcdefghijkl")                      # the re-list
    clock["t"] += 11
    assert f.idle_stops() == ["abcdefghijkl"], "re-listing reset the idle clock"


def test_a_container_the_fleet_never_saw_a_request_for_is_reclaimed():
    """last_activity 0.0 means "no request has ever been seen". Every start
    path records activity -- admit() on the two start branches, adopt() for a
    container found running at startup, and touch() for a start the gateway
    makes outside admit(), which is E2's first login, because a login is a
    user action. A container that arrives as RUNNING through none of those is
    something the gateway cannot account for, and it is reclaimed on the next
    sweep rather than kept forever."""
    _clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    assert f.idle_stops() == ["abcdefghijkl"]


def test_background_requests_alone_never_keep_a_container_past_fifteen_minutes():
    clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    f.touch("abcdefghijkl")                       # one real request
    for _ in range(200):
        clock["t"] += 10
        f.admit("abcdefghijkl", background=True)  # a poll, every ten seconds
    assert f.idle_stops() == ["abcdefghijkl"]


def test_a_real_request_keeps_it_awake():
    clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    f.touch("abcdefghijkl")
    clock["t"] += idle.IDLE_SECONDS - 1
    assert f.idle_stops() == []
    f.admit("abcdefghijkl", background=False)     # a real request, through admit
    clock["t"] += idle.IDLE_SECONDS - 1
    assert f.idle_stops() == []


def test_a_container_with_something_in_flight_is_never_stopped():
    """A turn can run longer than the idle window. Stopping mid-answer would
    look like a crash."""
    clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    f.touch("abcdefghijkl")
    f.enter("abcdefghijkl")
    clock["t"] += idle.IDLE_SECONDS * 3
    assert f.idle_stops() == []
    f.leave("abcdefghijkl")
    assert f.idle_stops() == ["abcdefghijkl"]


def test_at_the_cap_the_oldest_idle_container_is_evicted_first():
    """Design section 6 stopped only idle ones; the spec stops the running
    container with nothing in flight whose last non-background request is
    oldest."""
    clock, f = fleet(max_running=2)
    f.set_status("aaaaaaaaaaaa", idle.RUNNING)
    f.touch("aaaaaaaaaaaa")
    clock["t"] += 60
    f.set_status("bbbbbbbbbbbb", idle.RUNNING)
    f.touch("bbbbbbbbbbbb")
    clock["t"] += 60
    admission = f.admit("cccccccccccc", background=False)
    assert admission.action == "evict_then_start"
    assert admission.evict == "aaaaaaaaaaaa"


def test_a_busy_container_is_not_evicted_even_when_it_is_the_oldest():
    clock, f = fleet(max_running=2)
    f.set_status("aaaaaaaaaaaa", idle.RUNNING)
    f.touch("aaaaaaaaaaaa")
    f.enter("aaaaaaaaaaaa")
    clock["t"] += 60
    f.set_status("bbbbbbbbbbbb", idle.RUNNING)
    f.touch("bbbbbbbbbbbb")
    admission = f.admit("cccccccccccc", background=False)
    assert admission.action == "evict_then_start"
    assert admission.evict == "bbbbbbbbbbbb"


def test_with_nothing_evictable_the_answer_is_the_capacity_sentence():
    _clock, f = fleet(max_running=1)
    f.set_status("aaaaaaaaaaaa", idle.RUNNING)
    f.enter("aaaaaaaaaaaa")
    admission = f.admit("bbbbbbbbbbbb", background=False)
    assert admission.action == "at_capacity"
    assert admission.message == "At capacity, try again shortly."


def test_a_starting_container_counts_against_the_cap():
    """Otherwise a burst of first requests starts more containers than the VM
    has memory for, all at once."""
    _clock, f = fleet(max_running=1)
    f.set_status("aaaaaaaaaaaa", idle.STARTING)
    assert f.admit("bbbbbbbbbbbb", background=False).action == "at_capacity"


def test_a_background_request_at_the_cap_still_reads_paused():
    _clock, f = fleet(max_running=1)
    f.set_status("aaaaaaaaaaaa", idle.RUNNING)
    assert f.admit("bbbbbbbbbbbb", background=True).action == "paused"


def test_forgetting_a_tenant_removes_them_from_the_fleet():
    _clock, f = fleet()
    f.set_status("abcdefghijkl", idle.RUNNING)
    f.forget("abcdefghijkl")
    assert f.running() == []


def test_the_disk_warning_fires_at_eighty_percent():
    assert idle.disk_warning(79, 100) == ""
    assert "80" in idle.disk_warning(80, 100)
    assert "95" in idle.disk_warning(95, 100)
    assert idle.disk_warning(1, 0) == ""


def test_the_timeouts_are_the_specs_timeouts():
    assert idle.IDLE_SECONDS == 15 * 60
    assert idle.START_TIMEOUT_SECONDS == 15
    assert idle.FORWARD_TIMEOUT_SECONDS == 120
    assert idle.SESSION_CACHE_SECONDS == 60


def test_the_three_sentences_are_the_specs_sentences():
    """E3 and E4 render these. Pinned here so neither retypes them from the
    spec, which is how the paused sentence would have drifted if B4's contract
    test did not exist for it."""
    assert idle.CAPACITY_MESSAGE == "At capacity, try again shortly."
    assert idle.START_TIMEOUT_MESSAGE == (
        "Your assistant is taking too long to start. Try again.")
    assert idle.MAINTENANCE_MESSAGE == (
        "Your assistant is under maintenance. Try again in a few minutes.")


def test_reading_a_status_does_not_invent_a_container():
    """Fleet.running_status must not grow the fleet.

    `_state` is a setdefault, so a read through it creates a ContainerState
    for every tenant anybody asks about -- and `running()` counts STARTING and
    RUNNING, `idle_stops()` walks every entry, and the running cap is a length
    comparison against that walk. A probe that leaves a STOPPED entry behind
    is invisible until the day the dict is the thing being measured.
    """
    f = idle.Fleet(lambda: 0.0, max_running=1)
    assert f.running_status("aaaaaaaaaaaa") == idle.STOPPED
    # A private read, on purpose: "the fleet did not grow" has no public
    # expression on Fleet, and this is a shape check, not a guard.
    assert len(f._states) == 0
    assert f.running() == []
    assert f.idle_stops() == []
    f.set_status("bbbbbbbbbbbb", idle.RUNNING)
    assert f.running() == ["bbbbbbbbbbbb"]


def test_admit_settles_the_cap_without_any_help_from_the_caller():
    """Two admissions, no caller in between, one slot.

    `admit` decides AND reserves, so the second call must see a full fleet
    even though nothing has been stopped or started yet -- the caller of the
    first is still suspended on the spawner's socket. This is the property at
    the level it lives at: through the gateway, Launcher.stop happens to mark
    the evicted tenant STOPPED before its first await, so the gateway's own
    interleaving tests stay green with the evict-side line deleted. Here they
    do not.
    """
    _clock, f = fleet(max_running=1)
    f.adopt("aaaaaaaaaaaa")
    first = f.admit("bbbbbbbbbbbb", background=False)
    assert (first.action, first.evict) == ("evict_then_start", "aaaaaaaaaaaa")
    # Nothing has been stopped or started. The slot is still spoken for.
    second = f.admit("cccccccccccc", background=False)
    assert second.action == "at_capacity"
    assert second.message == idle.CAPACITY_MESSAGE
    # And the one being evicted is not offered up a second time.
    assert f.running() == ["bbbbbbbbbbbb"]


def test_a_reserved_slot_is_given_back_by_release_start():
    """release_start is what a refused start calls. It must move a STARTING
    tenant back to STOPPED and leave every other status alone -- a caller
    refused on its way to a container must not be able to record somebody
    else's RUNNING container as stopped."""
    _clock, f = fleet(max_running=2)
    assert f.admit("aaaaaaaaaaaa", background=False).action == "start"
    assert f.running() == ["aaaaaaaaaaaa"]
    f.release_start("aaaaaaaaaaaa")
    assert f.running() == []

    f.adopt("bbbbbbbbbbbb")
    f.release_start("bbbbbbbbbbbb")
    assert f.running() == ["bbbbbbbbbbbb"]     # RUNNING is not touched
    f.release_start("never-seen")              # and an unknown tenant is a no-op
    assert f.running() == ["bbbbbbbbbbbb"]
