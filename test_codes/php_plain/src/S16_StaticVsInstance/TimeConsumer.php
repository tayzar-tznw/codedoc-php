<?php
declare(strict_types=1);

namespace App\S16_StaticVsInstance;

class TimeConsumer
{
    public function tick(): array
    {
        $clock = new Clock();
        $cal = new Calendar();

        return [
            $clock->now(),
            Calendar::now(),
            $cal::now(),
        ];
    }
}
