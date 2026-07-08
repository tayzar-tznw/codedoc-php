<?php
declare(strict_types=1);

namespace App\S08_Magic;

class Engine
{
    public function start(): string
    {
        return 'engine-started';
    }

    public function status(): string
    {
        return 'engine-ok';
    }

    public function getTemperature(): int
    {
        return 90;
    }
}
