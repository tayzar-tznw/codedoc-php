<?php
declare(strict_types=1);

namespace App\S13_Functions;

function format_label(string $text): string
{
    return '[' . $text . ']';
}
