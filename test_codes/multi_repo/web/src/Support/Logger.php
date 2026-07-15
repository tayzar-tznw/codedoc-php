<?php
declare(strict_types=1);

namespace Web\Support;

// Deliberately shares the simple name "Logger" with Shared\Logging\Logger.
// This is a local, unrelated class — referencing it must NOT produce a
// cross-repo edge just because the names collide.
class Logger
{
    public function debug(string $message): void
    {
    }
}
