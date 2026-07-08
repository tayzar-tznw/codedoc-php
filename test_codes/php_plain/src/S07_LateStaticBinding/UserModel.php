<?php
declare(strict_types=1);

namespace App\S07_LateStaticBinding;

class UserModel extends ModelBase
{
    public static function name(): string
    {
        return 'user';
    }

    public static function boot(): string
    {
        return static::name();
    }
}
