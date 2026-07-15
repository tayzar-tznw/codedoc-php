<?php
declare(strict_types=1);

namespace Web\Time;

// Implements the shared contract via inline FQCN (no `use` import) — the
// heritage reference alone must produce the cross-repo `implements` edge.
final class FrozenClock implements \Shared\Contracts\Clock
{
    public function __construct(private int $timestamp)
    {
    }

    public function now(): int
    {
        return $this->timestamp;
    }
}
