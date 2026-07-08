<?php
declare(strict_types=1);

namespace App\S08_Magic;

class EngineFacade
{
    public static function __callStatic(string $method, array $args): mixed
    {
        return (new Engine())->$method(...$args);
    }
}
