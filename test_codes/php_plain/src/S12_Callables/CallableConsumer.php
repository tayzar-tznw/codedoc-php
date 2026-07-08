<?php
declare(strict_types=1);

namespace App\S12_Callables;

class CallableConsumer
{
    public function collect(TaskRunner $runner): array
    {
        $callables = [];
        $callables[] = [$runner, 'run'];
        $callables[] = 'App\\S12_Callables\\TaskRunner::compare';
        $callables[] = $runner->cleanup(...);
        $callables[] = TaskRunner::compare(...);
        $callables[] = [TaskRunner::class, 'compare'];

        return $callables;
    }
}
