<?php
declare(strict_types=1);

namespace App\S12_Callables;

class TaskRunner
{
    public function run(string $task): string
    {
        return 'ran:' . $task;
    }

    public function cleanup(): string
    {
        return 'cleaned';
    }

    public static function compare(int $a, int $b): int
    {
        return $a <=> $b;
    }
}
