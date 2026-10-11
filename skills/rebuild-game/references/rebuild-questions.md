# Reconstruction decisions

Use evidence before asking the user. Continue work already authorized in the
conversation; there is no mandatory generic scope checkpoint.

| Decision | Evidence or default | Ask only when |
|---|---|---|
| Engine version | Detected versions; report regional normalization | No version evidence or an explicit migration decision is needed |
| Scene identity | Serialized file, BuildSettings and bundle containers | Runtime state cannot be identified from available evidence |
| Packages | Signatures identify presence; versions remain unknown | A missing dependency cannot be resolved from available sources |
| Backend behavior | Code citations and observations | Credentials, unavailable content or desired replacement behavior blocks work |
| Reconstruction scope | Preserve discovered structure and report gaps | The user needs to choose between materially different behaviors |
| Missing content | Report file/reference scope and remote-content uncertainty | A required bundle or recording must be provided |

Do not auto-stub authentication, purchases, networking or saving into apparent
success. An offline replacement must be explicitly described and tested against
its intended behavior. Keep original evidence distinct from replacement code.

The default deliverable is a blueprint with scene/state maps, asset usage,
animations, script/dependency evidence and blockers. A structural Unity draft is
not proof of a playable reconstruction.
