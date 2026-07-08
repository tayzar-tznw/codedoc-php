<?php
declare(strict_types=1);

namespace App\S14_ConditionalTypes;

class JsonWriter
{
    public function write(array $rows): string
    {
        return 'json:' . count($rows);
    }
}
