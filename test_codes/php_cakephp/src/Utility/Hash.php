<?php
declare(strict_types=1);

namespace App\Utility;

class Hash
{
    public static function get(array $data, string $path, mixed $default = null): mixed
    {
        $current = $data;
        foreach (explode('.', $path) as $key) {
            if (!is_array($current) || !array_key_exists($key, $current)) {
                return $default;
            }
            $current = $current[$key];
        }

        return $current;
    }
}
