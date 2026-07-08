<?php
declare(strict_types=1);

namespace App\S16_StaticVsInstance;

class Calendar
{
    public static function now(): string
    {
        return 'calendar-now';
    }
}
