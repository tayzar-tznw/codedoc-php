<?php
declare(strict_types=1);

namespace App\S01_Aliases;

class FqcnConsumer
{
    public function buildAcme(): string
    {
        return (new \Acme\Reporting\Report())->generate();
    }

    public function buildGlobex(): string
    {
        return (new \Globex\Reporting\Report())->generate();
    }
}
