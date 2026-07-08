<?php
declare(strict_types=1);

namespace App\S16_StaticVsInstance;

class Clock
{
    public function now(): string
    {
        return 'clock-now';
    }
}
