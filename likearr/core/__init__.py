"""The pure core: data in, data out.

Every module here depends only on `likearr.models`, `likearr.ports`, `likearr.config` and the
standard library. No HTTP, no SQLite, no filesystem, no clock - `now` is always a parameter -
so the whole of likearr's decision-making is testable with dictionaries.

    normalize  title and name folding, shared by everything that compares strings
    resolver   source intents -> MusicBrainz release groups (the Singles rule)
    desire     resolutions -> the desired state
    diff       desired state x Lidarr x ownership -> the plan of record
    adopt      take responsibility for pre-existing monitoring
    prune      report files no source asks for any more
    explain    why a release is, or is not, monitored
"""
