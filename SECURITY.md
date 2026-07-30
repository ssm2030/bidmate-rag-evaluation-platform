# Security and data boundary

Do not commit real RFP, PDF, HWP, Office, archive, parsed document, `.env`, API key, model file, database, cache, export, or log. Runtime inputs belong in ignored local paths. The publication guard scans the worktree and every reachable Git blob; a failed or unavailable scan blocks release.

Report security issues privately to the repository owner through GitHub rather than opening an issue containing sensitive data.