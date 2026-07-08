<?php
declare(strict_types=1);

namespace App\S13_Functions\Sub;

class OtherNsConsumer
{
    public function resolve(string $text): string
    {
        return format_label($text);
    }
}
