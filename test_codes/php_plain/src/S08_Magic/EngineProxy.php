<?php
declare(strict_types=1);

namespace App\S08_Magic;

class EngineProxy
{
    public function __construct(private Engine $engine)
    {
    }

    public function __call(string $method, array $args): mixed
    {
        return $this->engine->$method(...$args);
    }

    public function __get(string $name): mixed
    {
        $getter = 'get' . ucfirst($name);

        return $this->engine->$getter();
    }

    public function stop(): string
    {
        return 'proxy-stopped';
    }
}
