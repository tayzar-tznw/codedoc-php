<?php
declare(strict_types=1);

namespace App\S07_LateStaticBinding;

abstract class ModelBase
{
    public static function make(): static
    {
        return new static();
    }

    public static function whoAmI(): string
    {
        return static::name();
    }

    public static function selfName(): string
    {
        return self::name();
    }

    public static function name(): string
    {
        return 'model';
    }
}
