# Third-party notices

## Psiphon Tunnel Core

Lumen's optional Psiphon integration supervises an **operator-supplied**
official Psiphon Tunnel Core ConsoleClient binary. This repository does not
include that binary, a Psiphon server entry, or any Psiphon credentials.

Psiphon Tunnel Core is licensed under GNU GPL version 3.0. Before distributing
an image that contains a Core binary or a derivative of Core, the operator
must independently meet all applicable GPL-3.0 notice, license-copy, source,
and corresponding-source obligations. Review the official project and its
license before enabling or bundling it:

- https://github.com/Psiphon-Labs/psiphon-tunnel-core
- https://github.com/Psiphon-Labs/psiphon-tunnel-core/blob/master/LICENSE

The supplied Lumen Docker image intentionally contains no Psiphon Core binary.

## Psiphon Tunnel Core

This distribution includes the unmodified supplied source archive of
`Psiphon-Labs/psiphon-tunnel-core` under `third_party/psiphon-tunnel-core` and
builds its official `ConsoleClient` entry point. Psiphon Tunnel Core is licensed
under GNU GPL v3; see `third_party/psiphon-tunnel-core/LICENSE`. The supplied
source is the corresponding source used by the container build. Lumen's Python
integration invokes the resulting program as a separate process.
