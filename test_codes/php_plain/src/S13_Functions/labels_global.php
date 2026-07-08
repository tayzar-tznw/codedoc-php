<?php
declare(strict_types=1);

function format_label(string $text): string
{
    return '<' . $text . '>';
}

function only_global(string $text): string
{
    return '{' . $text . '}';
}
