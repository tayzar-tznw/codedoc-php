<?php
declare(strict_types=1);

namespace App\S13_Functions;

class FunctionConsumer
{
    public function local(string $text): string
    {
        return format_label($text);
    }

    public function globalOnly(string $text): string
    {
        return \format_label($text);
    }

    public function fallback(string $text): string
    {
        return only_global($text);
    }
}
