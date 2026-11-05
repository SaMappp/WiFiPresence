# Independent repository migration

Date: 2026-11-05.

The canonical source is now [SaMappp/WiFiPresence](https://github.com/SaMappp/WiFiPresence). Previously, the project was a directory inside SaMappp/SaMappp.github.io. A GitHub directory has no separate repository description, issues, releases or contributor statistics. Those features belong to its parent repository.

The owner requested an independent project repository after comparing another course project. Saved course records request the team's own public repository; they do not establish a separate explicit requirement for a standalone repository. This migration does not claim instructor approval or that a repository link was sent to the instructor.

## Scope and history

- Extract only the projects/WiFiPresence subtree from the previously published source commit `b67ce683942efe99ec2f6479bd617a446948bc4c`.
- Preserve the three project commits, their authors, dates and messages. Subtree extraction changes commit IDs because file paths and parent trees change.
- Keep the eight previously authorized public NPZ files unchanged, including the failed/excluded pilot and diagnostic records. Existing public documentation and licenses remain.
- Publish no new recordings, trained-model parameters, private trial inventories, device metadata, local paths or private logs. Do not include unrelated personal-site files or history.
- Keep the existing [project website](https://samappp.github.io/projects/WiFiPresence/) as a static introduction and preview. Update its source links and mark the old source directory as an archival copy.

The repository description/topics can be configured independently. Contributor statistics follow actual Git authorship; team names in documentation do not create Git contributors. No release is fabricated as part of this structural migration.

## Verification boundary

The migration checks project file integrity against the published source, preserves historical raw-file hashes, and reruns offline/public-code tests. The added GitHub Actions workflow checks the same public software without private inputs. Private-data checks explicitly skip when the private local files are absent.

Local checks in the extracted repository on 2026-11-05: 55 root software tests passed; all eight historical public file hashes and dataset structure passed; the separate synthetic demonstration completed; public live checks passed 8 tests and explicitly skipped 5 private-data tests. These checks reused the existing Python/NumPy environment. A fresh dependency installation and real hardware were not tested.

This is a repository move, not a new sensing experiment. No ESP32 contact, firmware change, new capture, training or hardware restoration check is performed. Known empty-room false alarms, explicit UNKNOWN, quality gates and incomplete real-time hardware validation remain unchanged. Releases, contributor statistics and a successful workflow do not certify detection reliability.
