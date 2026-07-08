<?php
declare(strict_types=1);

namespace App\Utility;

class Text
{
    public static function slug(string $string, array|string $options = []): string
    {
        $replacement = is_array($options) ? ($options['replacement'] ?? '-') : $options;

        return strtolower(str_replace(' ', $replacement, trim($string)));
    }
}
