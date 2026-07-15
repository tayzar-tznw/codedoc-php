<?php
declare(strict_types=1);

namespace Web\Time;

use Shared\Contracts\Clock;

final class SystemClock implements Clock
{
    public function now(): int
    {
        return time();
    }
}
