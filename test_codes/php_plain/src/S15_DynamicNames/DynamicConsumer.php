<?php
declare(strict_types=1);

namespace App\S15_DynamicNames;

class DynamicConsumer
{
    public function dispatch(string $channel): string
    {
        $class = 'App\\S15_DynamicNames\\' . ucfirst($channel) . 'Handler';
        $handler = new $class();

        return $handler->handle();
    }

    public function build(string $channel): object
    {
        $class = 'App\\S15_DynamicNames\\' . ucfirst($channel) . 'Handler';

        return $class::create();
    }
}
